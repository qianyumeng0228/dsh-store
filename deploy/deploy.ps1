# deploy.ps1 —— 把 dist/site 部署到 GCP（ssh 别名 gcp，/var/www/dsh-store）
# 用法:
#   .\deploy\deploy.ps1                      # 默认: rsync 到 gcp:/var/www/dsh-store
#   .\deploy\deploy.ps1 -Host myhost         # 指定 ssh 别名
#   .\deploy\deploy.ps1 -Domain dsh.example.com   # 顺带在远端写好 Caddyfile 并 reload
param(
    [string]$Host = "gcp",
    [string]$Domain = "",
    [string]$RemoteDir = "/var/www/dsh-store"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Site = Join-Path $Root "dist\site"

if (-not (Test-Path -LiteralPath (Join-Path $Site "index.html"))) {
    throw "找不到站点产物: $Site —— 先运行 python pipeline/build.py"
}

Write-Host "==> 创建远端目录: ${Host}:${RemoteDir}"
ssh $Host "mkdir -p $RemoteDir"

Write-Host "==> 同步站点文件 (rsync，仅差异传输)"
# 远端一般有 rsync; 若没有, 先 apt install rsync
rsync -avz --delete "$Site/" "${Host}:${RemoteDir}/"

if ($Domain) {
    Write-Host "==> 写入 Caddyfile 并 reload"
    $caddy = @"
# DSH 插件商店 - 由 deploy.ps1 生成
$Domain {
    root * $RemoteDir
    file_server
    encode gzip zstd
    header Cache-Control "public, max-age=3600"
    @json path /index.json
    header @json Cache-Control "public, max-age=300"
    header X-Content-Type-Options "nosniff"
    header X-Frame-Options "SAMEORIGIN"
}
"@
    $tmp = Join-Path $env:TEMP "dsh-store-Caddyfile"
    [IO.File]::WriteAllText($tmp, $caddy + "`n", (New-Object Text.UTF8Encoding $false))
    scp $tmp "${Host}:/tmp/dsh-store-Caddyfile"
    ssh $Host "sudo sh -c 'cat /tmp/dsh-store-Caddyfile > /etc/caddy/Caddyfile.dsh-store && caddy validate --config /etc/caddy/Caddyfile.dsh-store && caddy reload --config /etc/caddy/Caddyfile' || echo 'Caddy reload 失败, 请手动检查'"
    Remove-Item $tmp -Force
}

Write-Host "==> 完成"
if ($Domain) {
    Write-Host "    索引源: https://$Domain/index.json"
    Write-Host "    网站:   https://$Domain/"
} else {
    Write-Host "    已同步到 ${Host}:${RemoteDir} (域名/Caddy 未配置)"
}
