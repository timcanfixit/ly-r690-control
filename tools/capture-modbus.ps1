<#
.SYNOPSIS
    Passively captures raw bytes from the RS485 tap (touchscreen <-> control board link) via
    the USB-serial adapter, to determine the actual baud rate and later decode live Modbus RTU
    traffic. READ-ONLY: this script never writes to the serial port, only listens.

.PARAMETER ComPort
    The Windows COM port the adapter enumerated as (e.g. "COM4").

.PARAMETER BaudRate
    Baud rate to try. Modbus RTU commonly uses 9600, 19200, 38400, 57600, or 115200.

.PARAMETER DurationSeconds
    How long to listen before stopping and writing the capture to disk. Default 30s.

.USAGE
    Run once per candidate baud rate while the machine is powered on and idling/running
    normally (so the touchscreen is actively polling the control board):
        .\tools\capture-modbus.ps1 -ComPort COM4 -BaudRate 9600
        .\tools\capture-modbus.ps1 -ComPort COM4 -BaudRate 19200
        ...
    Each run writes a timestamped .bin (raw bytes) and .txt (hex dump) into captures\.
    Whichever baud rate produces a hex dump that looks like clean, repeating Modbus RTU
    frames (not noise) is the right one.
#>
param(
    [string]$ComPort = "COM4",
    [int]$BaudRate = 9600,
    [int]$DurationSeconds = 30,
    [ValidateSet("None","Even","Odd")]
    [string]$Parity = "None",
    [int]$DataBits = 8,
    [ValidateSet("One","Two")]
    [string]$StopBits = "One"
)

$repoRoot = Split-Path -Parent $PSScriptRoot
$captureDir = Join-Path $repoRoot "captures"
New-Item -ItemType Directory -Force -Path $captureDir | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$baseName = "capture_${BaudRate}baud_${Parity}_${timestamp}"
$binPath = Join-Path $captureDir "$baseName.bin"
$hexPath = Join-Path $captureDir "$baseName.txt"

Write-Output "Opening $ComPort at $BaudRate baud, parity=$Parity, databits=$DataBits, stopbits=$StopBits"
Write-Output "Listening for $DurationSeconds seconds. READ-ONLY - nothing will be written to the port."

$port = New-Object System.IO.Ports.SerialPort $ComPort, $BaudRate, ([System.IO.Ports.Parity]::$Parity), $DataBits, ([System.IO.Ports.StopBits]::$StopBits)
$port.ReadTimeout = 500

$opened = $false
for ($attempt = 1; $attempt -le 5 -and -not $opened; $attempt++) {
    try {
        $port.Open()
        $opened = $true
    } catch [System.UnauthorizedAccessException] {
        if ($attempt -eq 5) { throw }
        Write-Output "Port busy (attempt $attempt/5), waiting for prior handle to release..."
        Start-Sleep -Milliseconds 800
    }
}

$allBytes = New-Object System.Collections.Generic.List[byte]
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    while ($sw.Elapsed.TotalSeconds -lt $DurationSeconds) {
        try {
            $b = $port.ReadByte()
            if ($b -ge 0) { $allBytes.Add([byte]$b) }
        } catch [System.TimeoutException] {
            # no data right now, keep waiting
        }
    }
} finally {
    $port.Close()
}

Write-Output "Captured $($allBytes.Count) bytes."

if ($allBytes.Count -gt 0) {
    [System.IO.File]::WriteAllBytes($binPath, $allBytes.ToArray())

    $sb = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt $allBytes.Count; $i += 16) {
        $chunk = $allBytes.GetRange($i, [Math]::Min(16, $allBytes.Count - $i))
        $hex = ($chunk | ForEach-Object { "{0:X2}" -f $_ }) -join ' '
        $ascii = -join ($chunk | ForEach-Object { if ($_ -ge 32 -and $_ -le 126) { [char]$_ } else { '.' } })
        $hexPadded = $hex.PadRight(48)
        $offsetStr = $i.ToString("D6")
        [void]$sb.AppendLine("${offsetStr}: ${hexPadded} ${ascii}")
    }
    [System.IO.File]::WriteAllText($hexPath, $sb.ToString())
    Write-Output "Wrote: $binPath"
    Write-Output "Wrote: $hexPath"
} else {
    Write-Output "No bytes captured - either wrong baud rate (framing errors dropped everything), the bus is idle, or the wiring needs to be checked."
}
