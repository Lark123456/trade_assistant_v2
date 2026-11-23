# 通达信同步诊断脚本(从您自己的终端运行,无沙箱限制)
Write-Host "=== 1. 检测通达信是否运行 ==="
$tdxProcs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
  $_.ProcessName -match "^(tdxw|hexin|TdxW|tdxwcn)$" -or $_.MainWindowTitle -match "通达信"
}
if ($tdxProcs) {
  $tdxProcs | Select-Object ProcessName, Id, StartTime, MainWindowTitle | Format-Table -AutoSize
  Write-Host "  通达信(类)进程数: $($tdxProcs.Count)" -Foreground Yellow
} else {
  Write-Host "  没找到通达信进程" -Foreground Green
}
Write-Host ""
Write-Host "=== 2. 从您自己的权限测试写入 E:\TDX\blocknew ==="
$probePath = "E:\TDX\T0002\blocknew\_userprobe_$PID.tmp"
try {
  [IO.File]::WriteAllText($probePath, "probe")
  Remove-Item $probePath -Force
  Write-Host "  写入成功 - 您的权限正常,问题不在文件系统" -Foreground Green
} catch {
  $e = $_.Exception.InnerException
  Write-Host "  写入失败: $($e.GetType().Name) - $($e.Message)" -Foreground Red
}
Write-Host ""
Write-Host "=== 3. 探测持锁进程(能看到的话) ==="
try {
  $lockers = openfiles /query /v 2>&1 | Select-String -Pattern "E:\\\\TDX|blocknew" | Select-Object -First 10
  if ($lockers) { $lockers | ForEach-Object { Write-Host $_ } }
  else { "  openfiles 无未枚举(需要管理员)" }
} catch { "  openfiles 不可用: $_" }
Write-Host ""
Write-Host "=== 4. E: 驱动器类型 ==="
try {
  $drv = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='E:'" -ErrorAction Stop
  "  类型: DriveType=$($drv.DriveType) (2=可移动 3=本地 4=网络) FileSystem=$($drv.FileSystem) Provider=$($drv.ProviderName)"
} catch { "  CIM 不可用: $_" }
