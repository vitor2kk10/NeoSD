# Baixa o instalador oficial do AnyFSE (github.com/ashpynov/AnyFSE, licença MIT) para empacotar junto com o NeoSD.
# Usado pelo build.bat. A licença do AnyFSE é baixada junto e vai no pacote (exigência da licença MIT);
# se ela não puder ser baixada, o instalador também não é incluído.
param([string]$Dest = "third_party\AnyFSE")
$ErrorActionPreference = 'Stop'
$h = @{ 'User-Agent' = 'NeoSD-build'; 'Accept' = 'application/vnd.github+json' }
try {
  New-Item -ItemType Directory -Force $Dest | Out-Null
  # releases/latest ignora versões beta; a lista inclui todas (a mais nova vem primeiro)
  $rels = Invoke-RestMethod 'https://api.github.com/repos/ashpynov/AnyFSE/releases?per_page=10' -Headers $h
  $rel = $rels | Where-Object { -not $_.draft -and ($_.assets | Where-Object { $_.name -like '*.exe' }) } | Select-Object -First 1
  if (-not $rel) { throw 'nenhuma versão com instalador encontrada' }
  $asset = $rel.assets | Where-Object { $_.name -like '*Installer*.exe' } | Select-Object -First 1
  if (-not $asset) { $asset = $rel.assets | Where-Object { $_.name -like '*.exe' } | Select-Object -First 1 }
  $lic = Join-Path $Dest 'LICENSE.txt'
  Invoke-WebRequest 'https://raw.githubusercontent.com/ashpynov/AnyFSE/HEAD/LICENSE' -OutFile $lic -UseBasicParsing -Headers @{ 'User-Agent' = 'NeoSD-build' }
  Invoke-WebRequest $asset.browser_download_url -OutFile (Join-Path $Dest 'AnyFSE.Installer.exe') -UseBasicParsing -Headers @{ 'User-Agent' = 'NeoSD-build' }
  @("AnyFSE $($rel.tag_name)$(if ($rel.prerelease) { ' (pré-lançamento)' })",
    "Arquivo original: $($asset.name)",
    "Página: $($rel.html_url)",
    "Código-fonte: https://github.com/ashpynov/AnyFSE",
    "Licença: MIT (texto completo em LICENSE.txt). Distribuído sem modificações.") | Set-Content (Join-Path $Dest 'SOURCE.txt') -Encoding UTF8
  Write-Host "      AnyFSE $($rel.tag_name) baixado ($($asset.name))."
  exit 0
} catch {
  Write-Host "      Não consegui baixar o AnyFSE: $($_.Exception.Message)"
  Remove-Item -Recurse -Force $Dest -ErrorAction SilentlyContinue
  exit 1
}
