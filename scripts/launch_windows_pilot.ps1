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
    $env:GITHUB_REF -cne 'refs/heads/codex/tideline-desktop-launch-pilot' -or
    $env:GITHUB_WORKFLOW_REF -cne 'linuxerlv/kpool-tideline-pilot/.github/workflows/windows-signature-pilot.yml@refs/heads/codex/tideline-desktop-launch-pilot' -or
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

    public static string OpenCommand()
    {
        uint length = 32768;
        var output = new StringBuilder((int)length);
        int result = AssocQueryStringW(0, 1, ".html", "open", output, ref length);
        return result == 0 ? output.ToString() : "";
    }

    public static void Changed()
    {
        SHChangeNotify(0x08000000, 0, IntPtr.Zero, IntPtr.Zero);
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
$className = 'KpoolTidelinePilot.' + [guid]::NewGuid().ToString('N')
$command = ('"{0}" --user-data-dir="{1}" --remote-debugging-address=127.0.0.1 ' +
    '--remote-debugging-port=0 --no-first-run --no-default-browser-check --disable-background-networking ' +
    '--disable-component-update --disable-sync --disable-extensions ' +
    '--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE localhost" "%1"') -f $chrome, $profile
$classes = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Classes', $true)
$originalCommand = [TidelineFileAssociation]::OpenCommand()
$oldExtension = $classes.OpenSubKey('.html', $true)
$oldDefault = $null
$oldDefaultKind = $null
if ($null -ne $oldExtension -and $oldExtension.GetValueNames() -contains '') {
    $oldDefault = $oldExtension.GetValue('', $null, 'DoNotExpandEnvironmentNames')
    $oldDefaultKind = $oldExtension.GetValueKind('')
}

$extension = $launcher = $null
$registeredClass = $registeredExtension = $false
$browserProof = $null

function Get-FixtureChrome {
    Get-CimInstance -ClassName Win32_Process -Filter "Name = 'chrome.exe' OR Name = 'chrome_crashpad_handler.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile, [StringComparison]::OrdinalIgnoreCase) }
}

try {
    # Windows still invokes the unmodified launcher's ordinary ShellExecute path.
    # The disposable runner's per-user registration gives its browser only a fresh profile and a loopback observer.
    $registeredClass = $true
    $openKey = $classes.CreateSubKey("$className\shell\open\command")
    try { $openKey.SetValue('', $command, 'String') } finally { $openKey.Dispose() }
    $registeredExtension = $true
    $extension = $classes.CreateSubKey('.html')
    $extension.SetValue('', $className, 'String')
    [TidelineFileAssociation]::Changed()
    if ([TidelineFileAssociation]::OpenCommand() -cne $command) {
        throw 'The runner does not resolve .html to the isolated Chromium fixture.'
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
        if ($registeredExtension) {
            if ($null -ne $oldDefaultKind) { $extension.SetValue('', $oldDefault, $oldDefaultKind) }
            elseif ($null -ne $extension) { $extension.DeleteValue('', $false) }
        }
        if ($null -ne $extension) { $extension.Dispose() }
        if ($registeredExtension -and $null -eq $oldExtension) { $classes.DeleteSubKey('.html', $false) }
        if ($null -ne $oldExtension) { $oldExtension.Dispose() }
        if ($registeredClass) { $classes.DeleteSubKeyTree($className, $false) }
        [TidelineFileAssociation]::Changed()
        if ([TidelineFileAssociation]::OpenCommand() -cne $originalCommand -or $classes.OpenSubKey($className)) {
            throw 'Fixture shell registration was not restored'
        }
    } catch { $cleanupErrors += 'fixture shell registration' }
    finally { $classes.Dispose() }

    if ($cleanupErrors.Count -gt 0) { throw "Failed to clean up $($cleanupErrors -join ', ')." }
}

$browserProof.processes_cleaned = $true
$browserProof.shell_association_restored = $true
$browserProof
