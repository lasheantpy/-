$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$projectPath = $PSScriptRoot
$exePath = Join-Path $projectPath 'dist\PaperHarbor\PaperHarbor.exe'
if (-not (Test-Path -LiteralPath $exePath)) { throw 'Executable missing' }
$desktopPath = [Environment]::GetFolderPath('Desktop')
$shellObj = New-Object -ComObject WScript.Shell
$shortcutPath = Join-Path $desktopPath '文献港 PaperHarbor.lnk'
if (Test-Path -LiteralPath $shortcutPath) {
  $existing = $shellObj.CreateShortcut($shortcutPath)
  if ($existing.TargetPath -ne $exePath) { throw 'A different shortcut already uses this name' }
}
foreach ($linkPath in @($shortcutPath, (Join-Path $projectPath '启动文献港.lnk'))) {
  $link = $shellObj.CreateShortcut($linkPath)
  $link.TargetPath = $exePath
  $link.WorkingDirectory = $projectPath
  $link.IconLocation = (Join-Path $projectPath 'assets\paperharbor.ico')
  $link.Description = '搜索数学论文，批量下载公开正文与补充材料'
  $link.WindowStyle = 1
  $link.Save()
}
$checkedLink = $shellObj.CreateShortcut($shortcutPath)
[PSCustomObject]@{desktop_shortcut=$shortcutPath;target=$checkedLink.TargetPath;working_directory=$checkedLink.WorkingDirectory;exists=(Test-Path -LiteralPath $shortcutPath)} | ConvertTo-Json -Compress
