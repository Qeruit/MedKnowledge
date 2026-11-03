param(
    [ValidateSet('Check', 'Build', 'Upgrade', 'Rollback')]
    [string]$Action = 'Check',
    [string]$ReleaseTag,
    [string]$PreviousTag,
    [switch]$Execute,
    [switch]$SchemaCompatible
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$stagingEnv = Join-Path $PSScriptRoot 'staging.env'
$composeFile = Join-Path $projectRoot 'compose.staging.yml'
$services = @('api', 'index-worker', 'quality-worker', 'ops', 'web')

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program exited with code $LASTEXITCODE"
    }
}

function Compose([string[]]$Arguments) {
    Invoke-Checked 'docker' (@('compose', '--env-file', $stagingEnv, '-f', $composeFile) + $Arguments)
}

function Require-CleanCommit {
    Invoke-Checked 'git' @('-C', $projectRoot, 'diff', '--check')
    $head = (& git -C $projectRoot rev-parse --short=12 HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -notmatch '^[0-9a-f]{12}$') {
        throw '先将项目提交到本地 Git，才能构建可追溯发布版本。'
    }
    $dirty = & git -C $projectRoot status --porcelain --untracked-files=all
    if ($LASTEXITCODE -ne 0 -or $dirty) {
        throw '工作区有未提交的变更；请先检查并提交，再进行版本构建。'
    }
    return "git-$head"
}

function Get-CurrentTag {
    $match = [regex]::Match([IO.File]::ReadAllText($stagingEnv), '(?m)^RELEASE_TAG=([^\r\n#]+)\s*$')
    if ($match.Success) { return $match.Groups[1].Value.Trim() }
    return 'staging'
}

function Assert-Tag([string]$Value) {
    if ($Value -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$') {
        throw '发布标签只能包含字母、数字、点、下划线和连字符，长度不超过 64。'
    }
}

function Set-CurrentTag([string]$Value) {
    Assert-Tag $Value
    $content = [IO.File]::ReadAllText($stagingEnv)
    if ($content -match '(?m)^RELEASE_TAG=') {
        $content = [regex]::Replace($content, '(?m)^RELEASE_TAG=[^\r\n]*', "RELEASE_TAG=$Value")
    } else {
        $content = $content.TrimEnd("`r", "`n") + "`nRELEASE_TAG=$Value`n"
    }
    $temp = Join-Path $PSScriptRoot ('.staging-env-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temp, $content, [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temp, $stagingEnv, $true)
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp }
    }
}

function Invoke-LocalChecks {
    Compose @('config', '--quiet')
    $env:RUN_UPLOAD_DB_TEST = '0'
    try {
        Push-Location (Join-Path $projectRoot 'backend')
        try { Invoke-Checked 'python' @('-m', 'pytest', 'tests', '-q') }
        finally { Pop-Location }
    } finally { Remove-Item Env:RUN_UPLOAD_DB_TEST -ErrorAction SilentlyContinue }
    Push-Location (Join-Path $projectRoot 'frontend')
    try { Invoke-Checked 'npm' @('run', 'build') }
    finally { Pop-Location }
}

if (-not (Test-Path -LiteralPath $stagingEnv)) {
    throw '缺少 deploy/staging.env；先按部署手册配置独立预发布环境。'
}

Push-Location $projectRoot
try {
    if ($Action -eq 'Check') {
        Invoke-LocalChecks
        Write-Host '本地检查完成；数据库集成测试和真实模型调用仍需独立验收。'
        return
    }

    if ($Action -eq 'Build' -or $Action -eq 'Upgrade') {
        $expectedTag = Require-CleanCommit
        if (-not $ReleaseTag) { $ReleaseTag = $expectedTag }
        if ($ReleaseTag -ne $expectedTag) {
            throw "此脚本只构建当前提交的标签 $expectedTag，防止同名镜像覆盖。"
        }
        Assert-Tag $ReleaseTag
        $env:RELEASE_TAG = $ReleaseTag
        Invoke-LocalChecks
        if ($Action -eq 'Build') {
            Compose @('build', 'api', 'web')
            Write-Host "镜像已构建：$ReleaseTag；未触碰运行中的容器。"
            return
        }
    }

    $currentTag = Get-CurrentTag
    if ($Action -eq 'Upgrade') {
        if (-not $PreviousTag -or $PreviousTag -ne $currentTag -or $ReleaseTag -eq $currentTag) {
            throw "-PreviousTag 必须等于当前 deploy/staging.env 的 RELEASE_TAG（$currentTag），且新旧标签不同。"
        }
        if (-not $Execute) {
            Write-Host "预演：从 $currentTag 升级到 $ReleaseTag。将先创建并验证备份，再构建新镜像、停止服务、手动迁移并等待健康检查。加 -Execute 才会修改运行环境。"
            return
        }
        $env:RELEASE_TAG = $currentTag
        $backupJson = (& docker compose --env-file $stagingEnv -f $composeFile exec -T api python -m scripts.backup create | Out-String)
        if ($LASTEXITCODE -ne 0) { throw '备份创建失败；未停止服务。' }
        $backup = $backupJson | ConvertFrom-Json
        if (-not $backup.id) { throw '备份未返回 ID；未停止服务。' }
        Compose @('exec', '-T', 'api', 'python', '-m', 'scripts.backup', 'verify', $backup.id)
        Write-Host "备份已核验：$($backup.id)"
        $env:RELEASE_TAG = $ReleaseTag
        Compose @('build', 'api', 'web')
        Compose (@('stop') + $services)
        try {
            Compose @('--profile', 'maintenance', 'run', '--rm', 'migrate')
            Compose (@('up', '-d', '--no-build', '--wait') + $services)
            Set-CurrentTag $ReleaseTag
            Write-Host "升级成功：$currentTag -> $ReleaseTag。备份 ID：$($backup.id)"
        } catch {
            Write-Error '升级未完成。没有自动回滚数据库迁移；请检查日志与备份，再决定兼容回退或隔离恢复。'
            throw
        }
        return
    }

    if ($Action -eq 'Rollback') {
        if (-not $PreviousTag -or $PreviousTag -eq $currentTag) {
            throw '请用 -PreviousTag 指定已有的旧镜像标签，且不能等于当前标签。'
        }
        Assert-Tag $PreviousTag
        if (-not $SchemaCompatible) {
            throw '只有确认数据库模式与旧代码向后兼容，才能执行代码回退；否则须先隔离恢复并制定切换方案。'
        }
        if (-not $Execute) {
            Write-Host "预演：从 $currentTag 回退到 $PreviousTag（仅代码镜像，不回滚数据库）。加 -Execute 才会执行。"
            return
        }
        Invoke-Checked 'docker' @('image', 'inspect', "medknowledge-backend:$PreviousTag")
        Invoke-Checked 'docker' @('image', 'inspect', "medknowledge-web:$PreviousTag")
        $env:RELEASE_TAG = $PreviousTag
        Compose (@('up', '-d', '--no-build', '--wait') + $services)
        Set-CurrentTag $PreviousTag
        Write-Host "代码回退完成：$currentTag -> $PreviousTag；数据库未回滚。"
    }
} finally {
    Remove-Item Env:RELEASE_TAG -ErrorAction SilentlyContinue
    Pop-Location
}
