# Fetch what CI made, then post one video. The whole local half of the loop.
#
# Run it by hand or from Task Scheduler; it is the same command either way.
#
#     .\tau\post.ps1              # the en channel
#     .\tau\post.ps1 -Channel en
#
# The channel is an argument rather than something you remember to export.
# Forgetting it in a shell does not fail - it silently runs the OTHER channel,
# which is on the api backend and writes into the tracked seen.db, and that is
# exactly the class of mistake this project has already made three times.
#
# publish.py decides on its own whether anything may go out now (daily count,
# gap, pause), so running this more often than the allowance is harmless.

param(
    [string]$Channel = "en"
)

$ErrorActionPreference = "Stop"

# Repo root, so a scheduled task's working directory does not matter.
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# The project venv explicitly: Task Scheduler runs with no venv activated, and
# the system python has none of this installed.
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "no interpreter at $Py" }

$env:OUTPUT_LANG = $Channel
# Python writes UTF-8 rather than the console codepage, or a Cyrillic caption
# in an error message comes back as mojibake.
$env:PYTHONIOENCODING = "utf-8"

$Log = Join-Path $Root "out\post.log"

function Say($msg) {
    "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg |
        Tee-Object -FilePath $Log -Append
}

# Redirection happens in cmd, NOT in PowerShell. `2>&1` on a native exe here
# wraps every stderr line in an ErrorRecord and trips $ErrorActionPreference -
# and publish.py logs its ordinary progress to stderr, so a completely normal
# run ("gap not elapsed") would come back looking like a crash.
# The script and its flags stay separate arguments; quoting them together
# would have cmd hand python one filename with a space and a dash in it.
function Run([string]$script, [string]$flags = "") {
    cmd /c ('"{0}" "{1}" {2} >> "{3}" 2>&1' -f $Py, $script, $flags, $Log)
    return $LASTEXITCODE
}

Say "--- $Channel ---"
$start = (Get-Content $Log -ErrorAction SilentlyContinue | Measure-Object -Line).Lines

# Not fatal on its own: the network may be down or gh logged out, and there can
# still be something already in out/ that is worth posting.
if ((Run "tau\fetch_ci_videos.py") -ne 0) { Say "fetch failed, carrying on" }

$code = Run "publish.py" "--next"

# cmd sent both commands' output to the file, so echo this run's share of it.
Get-Content $Log | Select-Object -Skip $start | Write-Output

if ($code -ne 0) {
    Say "publish.py exited $code"
    exit $code
}
