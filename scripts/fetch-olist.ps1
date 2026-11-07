# 下载公开 Olist 数据供本地非商业演示。
# 数据适用 CC BY-NC-SA 4.0 许可，下载内容不提交到 Git。
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$sourceDir = Join-Path $projectRoot 'data/source'
$archivePath = Join-Path $sourceDir 'olist-brazilian-ecommerce-v2.zip'
$extractDir = Join-Path $sourceDir 'olist'
$expectedHash = '967E41E04FC306FE604E2A693F488995A8B41E5047418F8A5C8E4ABD6DECA784'
$expectedFiles = @(
    'olist_customers_dataset.csv',
    'olist_geolocation_dataset.csv',
    'olist_order_items_dataset.csv',
    'olist_order_payments_dataset.csv',
    'olist_order_reviews_dataset.csv',
    'olist_orders_dataset.csv',
    'olist_products_dataset.csv',
    'olist_sellers_dataset.csv',
    'product_category_name_translation.csv'
)

New-Item -ItemType Directory -Force -Path $sourceDir | Out-Null

if (-not (Test-Path -LiteralPath $archivePath)) {
    $url = 'https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce?datasetVersionNumber=2'
    Invoke-WebRequest -Uri $url -OutFile $archivePath
}

$actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
if ($actualHash -ne $expectedHash) {
    throw "Olist archive checksum mismatch: expected $expectedHash, found $actualHash"
}

if (-not (Test-Path -LiteralPath $extractDir)) {
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractDir
}

foreach ($filename in $expectedFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $extractDir $filename))) {
        throw "Olist dataset is incomplete: $filename is missing"
    }
}

Write-Host "Olist version 2 is ready at $extractDir"
