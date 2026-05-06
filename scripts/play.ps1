param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$InputPath,

    [switch]$Play,
    [string]$RequireTitle = "Neverness",
    [double]$TempoScale = 1.0,
    [int]$Transpose = 0,
    [string]$LowestNote = "C3",
    [switch]$FitRange,
    [ValidateSet("skip", "octave-fold")]
    [string]$RangeMode = "skip",
    [ValidateSet("scan", "vk")]
    [string]$InputMode = "scan",
    [int]$LeadIn = 3,
    [switch]$ShowEvents
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$argsList = @(
    $InputPath,
    "--tempo-scale", "$TempoScale",
    "--transpose", "$Transpose",
    "--lowest-note", "$LowestNote",
    "--range-mode", $(if ($FitRange) { "octave-fold" } else { $RangeMode }),
    "--input-mode", "$InputMode"
)

if ($Play) {
    $argsList += @("--play", "--require-title", $RequireTitle, "--lead-in", "$LeadIn")
}
if ($ShowEvents) {
    $argsList += "--verbose"
}

python (Join-Path $root "nte_autoplayer.py") @argsList
