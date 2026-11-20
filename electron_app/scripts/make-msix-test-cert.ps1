# Create a self-signed code-signing cert for LOCAL MSIX testing only.
# Store submissions must NOT be signed — Partner Center signs packages itself.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\make-msix-test-cert.ps1
#
# Sideload the package after signing:
#   1. Import the .cer into Local Machine\Trusted People (admin):
#        certutil -addstore TrustedPeople %TEMP%\lp-msix-test.cer
#   2. Add-AppxPackage -Path package\LensPilot_<ver>_x64.msix
param(
    [string]$Subject = "CN=Dongwu259 Store Test",
    [string]$OutPfx  = "$env:TEMP\lp-msix-test.pfx",
    [string]$Password = "lp-test"
)
$ErrorActionPreference = "Stop"

$cert = New-SelfSignedCertificate -Type Custom `
    -Subject $Subject `
    -KeyUsage DigitalSignature `
    -FriendlyName "LensPilot MSIX Test" `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -NotAfter (Get-Date).AddYears(3) `
    -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")

$sec = ConvertTo-SecureString -String $Password -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath $OutPfx -Password $sec | Out-Null
Export-Certificate -Cert $cert -FilePath ($OutPfx -replace '\.pfx$', '.cer') | Out-Null

Write-Output "PFX=$OutPfx"
Write-Output "CER=$($OutPfx -replace '\.pfx$', '.cer')"
Write-Output "THUMBPRINT=$($cert.Thumbprint)"
Write-Output "SUBJECT=$Subject"
