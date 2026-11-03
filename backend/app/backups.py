"""Consistent pg_dump + referenced storage snapshots; isolated restore only."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import subprocess
import time
from uuid import UUID,uuid4

from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT
from app.db import db_session
from app.storage_guard import storage_lock

CORE_TABLES=("t_user","t_file_grant","t_ai_customer_service_md_storage","t_vector_store",
             "t_index_job","t_upload_session","t_upload_chunk")
FORMAT="medknowledge-backup-v1"


def digest_file(path):
    h=hashlib.sha256()
    with path.open("rb") as file:
        while block:=file.read(1024*1024): h.update(block)
    return h.hexdigest()


def table_digest(conn,table):
    assert table in CORE_TABLES
    rows=[]
    for row in conn.execute(text(f"SELECT * FROM {table}")).mappings():
        value=dict(row)
        if table=="t_ai_customer_service_md_storage": value.pop("file_path",None)
        rows.append(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str))
    return {"count":len(rows),"sha256":hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()}


def pg_tool(tool,args,database,settings,output=None,input_file=None):
    url=make_url(settings.database_url)
    env={**os.environ,"PGUSER":url.username or "postgres","PGPASSWORD":url.password or "",
         "PGDATABASE":database,"PGHOST":url.host or "127.0.0.1","PGPORT":str(url.port or 5432)}
    if settings.backup_pg_container:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+",settings.backup_pg_container): raise ValueError("容器名称无效")
        env["PGHOST"]="127.0.0.1"; env["PGPORT"]="5432"
        command=["docker","exec","-i"]
        for key in ("PGUSER","PGPASSWORD","PGDATABASE","PGHOST","PGPORT"): command += ["-e",key]
        command += [settings.backup_pg_container,tool,*args]
    else:
        binary=shutil.which(tool)
        if not binary: raise RuntimeError("未找到 PostgreSQL 工具，请配置 BACKUP_PG_CONTAINER 或安装同版本客户端")
        command=[binary,*args]
    result=subprocess.run(command,env=env,stdin=input_file,stdout=output if output else subprocess.PIPE,
                          stderr=subprocess.PIPE,timeout=1200)
    if result.returncode: raise RuntimeError(f"{tool} 执行失败（退出码 {result.returncode}），未输出连接凭据")


def relative_file(root,path):
    resolved=path.resolve()
    if path.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise RuntimeError("文件缺失、链接或路径越界，拒绝生成不完整备份")
    return resolved.relative_to(root).as_posix()


def create_backup():
    from app.db import settings
    started=time.perf_counter()
    backup_id=uuid4()
    root=settings.backup_root
    if root.is_relative_to(settings.md_storage_path): raise RuntimeError("备份目录不能位于资料存储目录内部")
    root.mkdir(parents=True,exist_ok=True)
    stage=root/("."+backup_id.hex+".partial")
    final=root/backup_id.hex
    stage.mkdir(exist_ok=False)
    with db_session() as db:
        db.execute(text("INSERT INTO t_backup_run(id,status) VALUES(:id,'running')"),{"id":backup_id})
    try:
        with storage_lock(exclusive=True) as conn:
            conn.execution_options(isolation_level="REPEATABLE READ")
            snapshot=conn.scalar(text("SELECT pg_export_snapshot()"))
            files=conn.execute(text("SELECT id,file_path FROM t_ai_customer_service_md_storage")).all()
            parts=conn.execute(text("""SELECT c.upload_id,c.chunk_index,c.chunk_size,c.chunk_sha256
                FROM t_upload_chunk c JOIN t_upload_session s ON s.id=c.upload_id WHERE s.status='uploading'""")).all()
            manifest={"format":FORMAT,"id":backup_id.hex,"database":settings.database_name,
                "createdAt":datetime.now().isoformat(),"files":[],"fileMap":{},
                "tables":{table:table_digest(conn,table) for table in CORE_TABLES}}
            wanted={}
            source_root=settings.md_storage_path.resolve()
            for row in files:
                relative=relative_file(source_root,Path(row.file_path))
                wanted[relative]=Path(row.file_path)
                manifest["fileMap"][str(row.id)]=relative
            for part in parts:
                path=source_root/".uploads"/str(part.upload_id)/f"{part.chunk_index:06d}.part"
                relative=relative_file(source_root,path)
                if path.stat().st_size!=part.chunk_size or digest_file(path)!=part.chunk_sha256:
                    raise RuntimeError("上传分片缺失或校验失败，拒绝备份")
                wanted[relative]=path
            for relative,path in sorted(wanted.items()):
                destination=stage/"storage"/relative
                destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,destination)
                sha=digest_file(destination)
                if sha!=digest_file(path): raise RuntimeError("复制过程中文件发生变化")
                manifest["files"].append({"path":relative,"bytes":destination.stat().st_size,"sha256":sha})
            with (stage/"database.dump").open("wb") as output:
                pg_tool("pg_dump",["--format=custom","--no-owner","--no-acl","--snapshot="+snapshot],
                        settings.database_name,settings,output=output)
            manifest["dumpSha256"]=digest_file(stage/"database.dump")
            manifest["bytes"]=(stage/"database.dump").stat().st_size+sum(item["bytes"] for item in manifest["files"])
            manifest["durationSeconds"]=time.perf_counter()-started
            (stage/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
            conn.commit()
        stage.rename(final)
        with db_session() as db:
            db.execute(text("""UPDATE t_backup_run SET status='succeeded',ended_at=clock_timestamp(),
                duration=:duration,bytes=:bytes,file_count=:files WHERE id=:id"""),
                {"id":backup_id,"duration":time.perf_counter()-started,"bytes":manifest["bytes"],"files":len(manifest["files"])})
        return manifest
    except Exception as exc:
        with db_session() as db:
            db.execute(text("""UPDATE t_backup_run SET status='failed',ended_at=clock_timestamp(),
                duration=:duration,detail=:detail WHERE id=:id"""),
                {"id":backup_id,"duration":time.perf_counter()-started,"detail":type(exc).__name__})
        raise


def verify_backup(backup_id):
    from app.db import settings
    identifier=UUID(str(backup_id)).hex
    folder=settings.backup_root/identifier
    if folder.is_symlink() or folder.resolve().parent!=settings.backup_root.resolve(): raise ValueError("备份目录越界")
    manifest=json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format")!=FORMAT or manifest.get("id")!=identifier: raise ValueError("备份清单身份不匹配")
    if digest_file(folder/"database.dump")!=manifest["dumpSha256"]: raise ValueError("数据库备份校验失败")
    if (folder/"database.dump").is_symlink(): raise ValueError("备份归档不能是链接")
    for item in manifest["files"]:
        relative=PurePosixPath(item["path"])
        if relative.is_absolute() or ".." in relative.parts or ":" in item["path"] or "\\" in item["path"]:
            raise ValueError("备份清单路径越界")
        path=folder/"storage"/item["path"]
        relative_file((folder/"storage").resolve(),path)
        if path.stat().st_size!=item["bytes"] or digest_file(path)!=item["sha256"]:
            raise ValueError("备份文件校验失败")
    if not set(manifest["fileMap"].values()).issubset({item["path"] for item in manifest["files"]}):
        raise ValueError("备份文件映射缺失")
    return folder,manifest


def restore_backup(backup_id,database):
    from app.db import settings
    if not re.fullmatch(r"agent_customer_service_restore_[a-f0-9]{12}",database):
        raise ValueError("恢复库必须为 agent_customer_service_restore_ 加 12 位十六进制，禁止覆盖业务库")
    source_url=make_url(settings.database_url)
    if database in (settings.database_name,make_url(settings.source_database_url).database): raise ValueError("拒绝覆盖原库")
    folder,manifest=verify_backup(backup_id)
    started=time.perf_counter()
    destination=(PROJECT_ROOT/"restores"/database).resolve()
    if destination.exists(): raise ValueError("恢复目录已存在，拒绝覆盖")
    admin=create_engine(source_url.set(database="postgres"),isolation_level="AUTOCOMMIT")
    created=False
    try:
        with admin.connect() as conn:
            if conn.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"),{"name":database}):
                raise ValueError("恢复库已存在，拒绝覆盖")
            conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
            created=True
        destination.mkdir(parents=True,exist_ok=False)
        if (folder/"storage").exists(): shutil.copytree(folder/"storage",destination/"storage")
        else: (destination/"storage").mkdir()
        with (folder/"database.dump").open("rb") as source:
            pg_tool("pg_restore",["--dbname="+database,"--no-owner","--no-acl","--exit-on-error"],
                    database,settings,input_file=source)
        engine=create_engine(source_url.set(database=database))
        try:
            with engine.begin() as conn:
                for table,expected in manifest["tables"].items():
                    if table_digest(conn,table)!=expected: raise RuntimeError(f"恢复数据核对失败：{table}")
                for file_id,relative in manifest["fileMap"].items():
                    conn.execute(text("UPDATE t_ai_customer_service_md_storage SET file_path=:path WHERE id=:id"),
                                 {"id":int(file_id),"path":str(destination/"storage"/relative)})
                conn.execute(text("DELETE FROM t_auth_session"))
                conn.execute(text("DELETE FROM t_login_limit"))
                conn.execute(text("DELETE FROM t_worker_heartbeat"))
                conn.execute(text("DELETE FROM t_ops_heartbeat"))
                conn.execute(text("UPDATE t_ops_alert SET state='clear',first_seen=NULL"))
                conn.execute(text("UPDATE t_backup_run SET status='interrupted',ended_at=clock_timestamp() WHERE status='running'"))
                conn.execute(text("""UPDATE t_index_job SET claim_token=NULL,worker_id=NULL,
                    lease_until=clock_timestamp()-interval '1 second',deadline_at=clock_timestamp()-interval '1 second'
                    WHERE status='running'"""))
        finally: engine.dispose()
        report={"database":database,"storage":str(destination/"storage"),"backupId":manifest["id"],
                "durationSeconds":time.perf_counter()-started,"verifiedTables":manifest["tables"],
                "verifiedFiles":len(manifest["files"]),"sessionsRevoked":True,"workerHeartbeatsCleared":True}
        (destination/"restore-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        return report
    except Exception:
        # Preserve a failed NEW restore for diagnosis; never delete/overwrite user DBs.
        raise
    finally: admin.dispose()


def prune_backups():
    from app.db import settings
    entries=[]
    for folder in settings.backup_root.iterdir():
        if not folder.is_dir() or not re.fullmatch(r"[a-f0-9]{32}",folder.name): continue
        try:
            _,manifest=verify_backup(folder.name)
            if manifest["database"]==settings.database_name: entries.append((manifest["createdAt"],folder))
        except Exception: continue
    removed=[]
    for _,folder in sorted(entries,reverse=True)[settings.backup_keep_count:]:
        if folder.resolve().parent!=settings.backup_root.resolve() or folder.is_symlink(): raise ValueError("备份清理路径越界")
        shutil.rmtree(folder)
        removed.append(folder.name)
    return removed


def backup_due():
    from app.db import settings
    with db_session() as db:
        row=db.execute(text("SELECT status,started_at,ended_at FROM t_backup_run ORDER BY started_at DESC LIMIT 1")).mappings().first()
        success=db.scalar(text("""SELECT EXTRACT(EPOCH FROM(clock_timestamp()-max(ended_at)))
            FROM t_backup_run WHERE status='succeeded'"""))
        now=db.scalar(text("SELECT clock_timestamp()::timestamp"))
    if row and (now-row["started_at"]).total_seconds()<settings.backup_retry_seconds: return False
    return success is None or float(success)>=settings.backup_interval_hours*3600
