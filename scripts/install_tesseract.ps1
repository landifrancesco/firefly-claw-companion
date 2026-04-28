Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (Get-Command tesseract -ErrorAction SilentlyContinue) {
    Write-Host "tesseract is already installed."
    exit 0
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is required to install tesseract automatically on Windows."
}

winget install -e --id tesseract-ocr.tesseract --accept-package-agreements --accept-source-agreements
