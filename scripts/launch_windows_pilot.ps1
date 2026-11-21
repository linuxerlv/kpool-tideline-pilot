[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $Download,
    [Parameter(Mandatory)][string] $ExpectedSha256,
    [Parameter(Mandatory)][string] $BundleSha256,
    [Parameter(Mandatory)][string] $Scratch,
    [Parameter(Mandatory)][string] $Screenshot
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or
    $env:RUNNER_OS -cne 'Windows' -or $env:RUNNER_ARCH -cne 'X64' -or
    $env:GITHUB_EVENT_NAME -cne 'workflow_dispatch' -or
    $env:GITHUB_REPOSITORY -cne 'linuxerlv/kpool-tideline-pilot' -or
    $env:GITHUB_REF -cne 'refs/heads/codex/tideline-desktop-launch-v2' -or
    $env:GITHUB_WORKFLOW_REF -cne 'linuxerlv/kpool-tideline-pilot/.github/workflows/windows-signature-pilot.yml@refs/heads/codex/tideline-desktop-launch-v2' -or
    $env:GITHUB_SHA -cnotmatch '\A[0-9a-f]{40}\z' -or $env:GITHUB_WORKFLOW_SHA -cne $env:GITHUB_SHA) {
    throw 'Desktop launch proof requires the designated hosted Windows pilot.'
}

if ((Get-FileHash -LiteralPath $Download -Algorithm SHA256).Hash -cne $ExpectedSha256 -or
    (Get-AuthenticodeSignature -LiteralPath $Download).Status -ne 'Valid') {
    throw 'Refusing to launch bytes that differ from the verified signed download.'
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class TidelineFileAssociation
{
    [DllImport("shell32.dll")]
    public static extern void SHChangeNotify(uint eventId, uint flags, IntPtr first, IntPtr second);

    [DllImport("shlwapi.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int AssocQueryStringW(uint flags, uint textType, string extension,
        string verb, StringBuilder output, ref uint outputLength);

    public static string Query(uint type, string verb)
    {
        uint length = 32768;
        var output = new StringBuilder((int)length);
        int result = AssocQueryStringW(0, type, ".html", String.IsNullOrEmpty(verb) ? null : verb, output, ref length);
        return result == 0 ? output.ToString() : "";
    }

    public static void Changed()
    {
        // SHCNE_ASSOCCHANGED and SHCNF_FLUSH.
        SHChangeNotify(0x08000000, 0x1000, IntPtr.Zero, IntPtr.Zero);
    }
}
'@

$chrome = @($env:ProgramFiles, [Environment]::GetEnvironmentVariable('ProgramFiles(x86)') |
    ForEach-Object { Join-Path $_ 'Google\Chrome\Application\chrome.exe' } |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }) | Select-Object -First 1
if (-not $chrome) { throw 'The hosted Windows runner has no Chrome installation.' }

$profile = Join-Path $Scratch 'chrome-profile'
$unpacked = Join-Path $Scratch 'launcher-output'
New-Item -ItemType Directory -Path $unpacked | Out-Null
$command = ('"{0}" --user-data-dir="{1}" --remote-debugging-address=127.0.0.1 ' +
    '--remote-debugging-port=0 --no-first-run --no-default-browser-check --disable-background-networking ' +
    '--disable-component-update --disable-sync --disable-extensions ' +
    '--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE localhost" "%1"') -f $chrome, $profile
$className = [TidelineFileAssociation]::Query(20, $null)  # ASSOCSTR_PROGID
$originalCommand = [TidelineFileAssociation]::Query(1, 'open')  # ASSOCSTR_COMMAND
$delegate = [TidelineFileAssociation]::Query(18, 'open')  # ASSOCSTR_DELEGATEEXECUTE
if ($className -cnotmatch '\A[A-Za-z][A-Za-z0-9._-]{0,120}\z' -or
    $originalCommand -notmatch '^"[^"]+\\(?:chrome|msedge|firefox|iexplore)\.exe"(?:\s|$)' -or $delegate) {
    throw 'The hosted runner must resolve .html to a registered Win32 browser command.'
}

$openPath = "Software\Classes\$className\shell\open"
$handler = $handlerHive = $launcher = $null
$commandReplaced = $false
$browserProof = $null

function Get-FixtureChrome {
    Get-CimInstance -ClassName Win32_Process -Filter "Name = 'chrome.exe' OR Name = 'chrome_crashpad_handler.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile, [StringComparison]::OrdinalIgnoreCase) }
}

try {
    # Windows still invokes the unmodified launcher's ordinary ShellExecute path.
    # Match the selected ProgID's exact Windows command. UserChoice remains intact.
    foreach ($hive in @([Microsoft.Win32.Registry]::CurrentUser, [Microsoft.Win32.Registry]::LocalMachine)) {
        $candidate = $hive.OpenSubKey("$openPath\command", $true)
        if ($null -eq $candidate) { continue }
        if ($candidate.GetValue('') -ceq $originalCommand) {
            $handler = $candidate
            $handlerHive = $hive
            break
        }
        $candidate.Dispose()
    }
    if ($null -eq $handler) { throw "No exact registry command found for the selected .html browser $className." }
    $openKey = $handlerHive.OpenSubKey($openPath)
    try {
        if ($openKey.GetSubKeyNames() -icontains 'ddeexec' -or $openKey.GetSubKeyNames() -icontains 'DropTarget') {
            throw "The selected .html browser $className uses a non-command shell handler."
        }
    } finally { $openKey.Dispose() }
    $oldCommand = $handler.GetValue('', $null, 'DoNotExpandEnvironmentNames')
    $oldCommandKind = $handler.GetValueKind('')
    $commandReplaced = $true
    $handler.SetValue('', $command, 'String')
    [TidelineFileAssociation]::Changed()
    if ([TidelineFileAssociation]::Query(20, $null) -cne $className -or
        [TidelineFileAssociation]::Query(1, 'open') -cne $command) {
        throw "The selected .html browser $className did not resolve to the fixture command."
    }

    $start = [Diagnostics.ProcessStartInfo]::new($Download)
    $start.UseShellExecute = $false
    $start.WorkingDirectory = $Scratch
    $start.Environment['TEMP'] = $unpacked
    $start.Environment['TMP'] = $unpacked
    $launcher = [Diagnostics.Process]::Start($start)
    $launcherId = $launcher.Id
    if (-not $launcher.WaitForExit(15000)) { throw 'The downloaded launcher did not exit.' }
    if ($launcher.ExitCode -ne 0) { throw "The downloaded launcher exited with $($launcher.ExitCode)." }

    $outputs = @(Get-ChildItem -LiteralPath $unpacked -Force)
    if ($outputs.Count -ne 1 -or -not $outputs[0].PSIsContainer -or
        $outputs[0].Name -cnotmatch '\ATideline-[0-9a-f]{32}\z' -or
        ($outputs[0].Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Expected a fresh game directory created by the downloaded launcher.'
    }
    $game = Join-Path $outputs[0].FullName 'Tideline.html'
    if ((Get-FileHash -LiteralPath $game -Algorithm SHA256).Hash.ToLowerInvariant() -cne $BundleSha256) {
        throw 'The launcher-created game differs from the embedded bundle.'
    }

    $observed = & node (Join-Path $PSScriptRoot 'observe_windows_game.mjs') $profile $game $Screenshot
    if ($LASTEXITCODE -ne 0) { throw 'The launched Chromium tab did not pass the game scenario.' }
    $browserProof = $observed | ConvertFrom-Json -AsHashtable
    $gameProcesses = @(Get-FixtureChrome | Where-Object {
        $_.Name -ceq 'chrome.exe' -and
        $_.CommandLine.Contains($game, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($gameProcesses.Count -ne 1) { throw 'Expected one isolated Chrome process with the launcher-created file argument.' }
    $browserProof.browser_pid = [int] $gameProcesses[0].ProcessId
    $browserProof.browser_parent_pid = [int] $gameProcesses[0].ParentProcessId
    $browserProof.launcher_pid = $launcherId
    $browserProof.launcher_exit_code = $launcher.ExitCode
    $browserProof.extracted_sha256 = $BundleSha256
    $browserProof.shell_association = 'isolated-chromium'
} finally {
    $cleanupErrors = @()
    try {
        if ($null -ne $launcher) {
            if (-not $launcher.HasExited) {
                $launcher.Kill($true)
                if (-not $launcher.WaitForExit(5000)) { throw 'Launcher still running' }
            }
            $launcher.Dispose()
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(8)
        do {
            $remaining = @(Get-FixtureChrome)
            foreach ($process in $remaining) {
                Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
            }
            if ($remaining.Count) { Start-Sleep -Milliseconds 150 }
        } while ($remaining.Count -and [DateTime]::UtcNow -lt $deadline)
        if ((@(Get-FixtureChrome)).Count) { throw 'Chrome fixture still running' }
    } catch { $cleanupErrors += 'fixture processes' }

    try {
        if ($commandReplaced) {
            $handler.SetValue('', $oldCommand, $oldCommandKind)
            [TidelineFileAssociation]::Changed()
            if ($handler.GetValue('', $null, 'DoNotExpandEnvironmentNames') -cne $oldCommand -or
                $handler.GetValueKind('') -ne $oldCommandKind -or
                [TidelineFileAssociation]::Query(20, $null) -cne $className -or
                [TidelineFileAssociation]::Query(1, 'open') -cne $originalCommand) {
                throw 'Fixture shell registration was not restored'
            }
        }
    } catch { $cleanupErrors += 'fixture shell registration' }
    finally { if ($null -ne $handler) { $handler.Dispose() } }

    if ($cleanupErrors.Count -gt 0) { throw "Failed to clean up $($cleanupErrors -join ', ')." }
}

$browserProof.processes_cleaned = $true
$browserProof.shell_association_restored = $true
$browserProof
