"""Trusted operator CLI. Dumps contain private data; do not commit/share them."""
import argparse
import json

from app.backups import create_backup,restore_backup,verify_backup,prune_backups


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("create")
    verify=sub.add_parser("verify"); verify.add_argument("backup_id")
    restore=sub.add_parser("restore"); restore.add_argument("backup_id"); restore.add_argument("database")
    sub.add_parser("prune")
    args=parser.parse_args()
    if args.command=="create":
        manifest=create_backup()
        result={key:manifest[key] for key in ("id","bytes","durationSeconds")}
    elif args.command=="verify":
        _,manifest=verify_backup(args.backup_id)
        result={"id":manifest["id"],"filesVerified":len(manifest["files"])}
    elif args.command=="restore": result=restore_backup(args.backup_id,args.database)
    else: result={"removed":prune_backups()}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
