# NeoSD - diagnóstico do AnyFSE
# Mostra onde o AnyFSE guarda a configuração do launcher escolhido, para o NeoSD poder se registrar
# sozinho numa versão futura. Só LÊ: não altera nada.
# Como usar: configure o AnyFSE uma vez (escolha qualquer executável personalizado) e depois rode:
#   powershell -ExecutionPolicy Bypass -File tools\anyfse-diagnostico.ps1
# O resultado vai para a Área de Trabalho (anyfse-diagnostico.txt).

$out = Join-Path ([Environment]::GetFolderPath('Desktop')) 'anyfse-diagnostico.txt'
$log = New-Object System.Collections.Generic.List[string]
function W($s) { $log.Add([string]$s) }

W "== NeoSD - diagnostico AnyFSE  $(Get-Date -Format s)"
W "Windows: $([Environment]::OSVersion.VersionString)"

W "`n== Pastas"
foreach ($d in @("$env:ProgramFiles\AnyFSE", "$env:ProgramData\AnyFSE", "$env:LOCALAPPDATA\AnyFSE", "$env:APPDATA\AnyFSE")) {
  if (Test-Path $d) {
    W "[existe] $d"
    Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 80 | ForEach-Object {
      W ("   {0}  ({1} bytes, {2:s})" -f $_.FullName, $_.Length, $_.LastWriteTime)
      if ($_.Extension -in '.json', '.ini', '.xml', '.config', '.cfg', '.txt' -and $_.Length -lt 20000) {
        W "   ---- conteudo:"; Get-Content $_.FullName -ErrorAction SilentlyContinue | ForEach-Object { W "     $_" }
      }
    }
  } else { W "[nao existe] $d" }
}

W "`n== Registro"
foreach ($k in @('HKCU:\Software\AnyFSE', 'HKLM:\SOFTWARE\AnyFSE', 'HKLM:\SOFTWARE\WOW6432Node\AnyFSE',
                 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\AnyFSE')) {
  if (Test-Path $k) {
    W "[existe] $k"
    Get-ChildItem $k -Recurse -ErrorAction SilentlyContinue | ForEach-Object { W "  subchave: $($_.Name)" }
    foreach ($item in @(Get-Item $k) + @(Get-ChildItem $k -Recurse -ErrorAction SilentlyContinue)) {
      foreach ($n in $item.GetValueNames()) { W ("  {0} :: {1} = {2}" -f $item.Name, $n, $item.GetValue($n)) }
    }
  } else { W "[nao existe] $k" }
}

W "`n== Pacote (modo Xbox)"
Get-AppxPackage -Name '*AnyFSE*' -ErrorAction SilentlyContinue | ForEach-Object { W "$($_.Name)  $($_.Version)  $($_.InstallLocation)" }

$log | Set-Content -Path $out -Encoding UTF8
Write-Host "Pronto: $out"
Write-Host "Revise o arquivo (ele pode conter caminhos do seu PC) e envie para quem for automatizar a integracao."
