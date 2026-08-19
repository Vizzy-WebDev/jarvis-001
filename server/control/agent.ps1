# The computer-control primitive bridge. Reads one JSON command per line on
# stdin, writes one JSON result per line on stdout, and keeps running until
# stdin closes — see ps-bridge.js for why this is one long-lived process
# rather than one process per action (a cold powershell.exe start costs
# ~300ms; a session doing 40 actions would waste 12 seconds on process
# startup alone).
#
# Launched with -STA (see ps-bridge.js) — UI Automation and the clipboard
# both expect single-threaded apartment COM, and PowerShell's default is MTA.
#
# Every command result is `{id, ok, result}` or `{id, ok:false, error}` so
# ps-bridge.js can match responses back to the request that asked for them
# even though everything shares one stdin/stdout pair.

# ---PARAM-BLOCK-START---
param()
# ---PARAM-BLOCK-END---

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;

public class Native {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    // PostMessage (not SendMessage) for WM_CLOSE — asynchronous, so a window
    // that's slow to respond (or shows an "unsaved changes?" prompt of its
    // own) never blocks this bridge waiting for it. This is the same signal
    // clicking a window's own X button sends; the target application decides
    // for itself whether to actually close, exactly as a real user's click
    // would. There is deliberately no process-kill command anywhere in this
    // file — see CLAUDE.md's Notepad shared-process lesson for why.
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    // dwData is declared `int`, not the DWORD-matching `uint` the Win32 header
    // uses — this is our own P/Invoke signature, not a fixed external
    // contract, and mouse_event only cares about the bit pattern it's given
    // (a negative wheel delta scrolls down). Declaring it `uint` forces a
    // checked negative-to-uint32 cast on the PowerShell side that throws.
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, int dx, int dy, int dwData, IntPtr dwExtraInfo);
    [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
}
"@ -ReferencedAssemblies 'System.Windows.Forms'

$MOUSEEVENTF_LEFTDOWN = 0x0002
$MOUSEEVENTF_LEFTUP = 0x0004
$MOUSEEVENTF_RIGHTDOWN = 0x0008
$MOUSEEVENTF_RIGHTUP = 0x0010
$MOUSEEVENTF_WHEEL = 0x0800
$SW_RESTORE = 9
$SW_MINIMIZE = 6
$WM_CLOSE = 0x0010
# SetWindowPos flags for arrange_window — position/size only, never touch
# Z-order or activation (SWP_NOZORDER/SWP_NOACTIVATE), so arranging a window
# side-by-side never also steals focus or reorders unrelated windows.
$SWP_NOZORDER = 0x0004
$SWP_NOACTIVATE = 0x0010

# ---------- windows ----------

function Get-WindowList {
    $results = New-Object System.Collections.Generic.List[object]
    $foreground = [Native]::GetForegroundWindow()
    $callback = {
        param($hWnd, $lParam)
        if (-not [Native]::IsWindowVisible($hWnd)) { return $true }
        $len = [Native]::GetWindowTextLength($hWnd)
        if ($len -eq 0) { return $true }
        $sb = New-Object System.Text.StringBuilder ($len + 1)
        [Native]::GetWindowText($hWnd, $sb, $sb.Capacity) | Out-Null
        $title = $sb.ToString()
        if ([string]::IsNullOrWhiteSpace($title)) { return $true }
        $procId = 0
        [Native]::GetWindowThreadProcessId($hWnd, [ref]$procId) | Out-Null
        $procName = ''
        try { $procName = (Get-Process -Id $procId -ErrorAction Stop).ProcessName } catch {}
        $rect = New-Object Native+RECT
        [Native]::GetWindowRect($hWnd, [ref]$rect) | Out-Null
        $script:windowResults.Add([PSCustomObject]@{
            handle    = $hWnd.ToInt64().ToString()
            title     = $title
            processName = $procName
            foreground  = ($hWnd -eq $script:windowResultsForeground)
            bounds    = @{ left = $rect.Left; top = $rect.Top; right = $rect.Right; bottom = $rect.Bottom }
        })
        return $true
    }
    # EnumWindows' callback closure can't see script-scope $results reliably
    # across the delegate boundary in every PowerShell version — routed
    # through explicit script-scope variables instead of relying on closure
    # capture, which is the one part of this that's genuinely finicky.
    $script:windowResults = $results
    $script:windowResultsForeground = $foreground
    [Native]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
    return $script:windowResults
}

# ---------- UI Automation (read_window) ----------

function Get-ElementInfo($el, $depth, $maxDepth, $out, $cap) {
    if ($out.Count -ge $cap -or $depth -gt $maxDepth) { return }
    try {
        $rect = $el.Current.BoundingRectangle
        $name = $el.Current.Name
        $role = $el.Current.LocalizedControlType
        $enabled = $el.Current.IsEnabled
        $value = $null
        try {
            $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            if ($vp) { $value = $vp.Current.Value }
        } catch {}
        # Skip elements with no name and no bounds — almost always structural
        # containers that add noise, not something a click target would ever
        # need to reference.
        if (-not [string]::IsNullOrWhiteSpace($name) -or ($rect.Width -gt 0 -and $rect.Height -gt 0)) {
            $out.Add([PSCustomObject]@{
                id      = $out.Count
                role    = $role
                name    = $name
                value   = $value
                enabled = $enabled
                bounds  = @{ left = [int]$rect.Left; top = [int]$rect.Top; right = [int]$rect.Right; bottom = [int]$rect.Bottom }
            })
        }
    } catch {}
    if ($out.Count -ge $cap) { return }
    try {
        $children = $el.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($child in $children) {
            Get-ElementInfo $child ($depth + 1) $maxDepth $out $cap
            if ($out.Count -ge $cap) { return }
        }
    } catch {}
}

function Read-WindowTree($handleStr) {
    $handle = [IntPtr]::new([int64]$handleStr)
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
    if (-not $root) { throw "No such window (it may have closed)." }
    $out = New-Object System.Collections.Generic.List[object]
    # Depth 6 / 300 elements is generous for an ordinary dialog or document
    # window without risking a multi-second walk on something pathological
    # (a huge spreadsheet grid, a deeply nested web page rendered as native
    # controls) — this is the "accurate aim" path, not an exhaustive dump.
    Get-ElementInfo $root 0 6 $out 300
    return $out
}

# ---------- screenshot ----------

function Take-Screenshot($handleStr, $maxWidth) {
    if ($handleStr) {
        $handle = [IntPtr]::new([int64]$handleStr)
        $rect = New-Object Native+RECT
        [Native]::GetWindowRect($handle, [ref]$rect) | Out-Null
        $x = $rect.Left; $y = $rect.Top
        $w = $rect.Right - $rect.Left; $h = $rect.Bottom - $rect.Top
    } else {
        $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $x = $screen.X; $y = $screen.Y; $w = $screen.Width; $h = $screen.Height
    }
    if ($w -le 0 -or $h -le 0) { throw "That window has no visible area to capture." }

    $bitmap = New-Object System.Drawing.Bitmap $w, $h
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($x, $y, 0, 0, (New-Object System.Drawing.Size $w, $h))
    $graphics.Dispose()

    $final = $bitmap
    if (-not $maxWidth) { $maxWidth = 1280 }
    if ($w -gt $maxWidth) {
        $scale = $maxWidth / $w
        $newW = [int]$maxWidth
        $newH = [int]($h * $scale)
        $resized = New-Object System.Drawing.Bitmap $bitmap, $newW, $newH
        $bitmap.Dispose()
        $final = $resized
    }

    $stream = New-Object System.IO.MemoryStream
    $final.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bytes = $stream.ToArray()
    $stream.Dispose()
    $final.Dispose()
    return [Convert]::ToBase64String($bytes)
}

# ---------- cursor movement ----------
# A synthetic click used to set Cursor::Position directly — instant, so the
# cursor teleported straight to the target with nothing visible in between.
# Per the requirement that the user be able to see where Jarvis is clicking,
# every click now glides there first over a short series of eased steps. The
# final step always lands exactly on the requested point, so session.js's
# post-click cursor-drift guard (which compares against the last commanded
# position) is unaffected.
function Move-CursorSmoothly($targetX, $targetY) {
    $start = [System.Windows.Forms.Cursor]::Position
    $steps = 20
    $durationMs = 250
    $stepDelay = [int]($durationMs / $steps)
    for ($i = 1; $i -le $steps; $i++) {
        # Ease-out (quadratic) — fast start, gentle settle, reads as a real
        # hand movement rather than a linear robotic slide.
        $t = $i / [double]$steps
        $eased = 1 - [Math]::Pow(1 - $t, 2)
        $x = [int]($start.X + ($targetX - $start.X) * $eased)
        $y = [int]($start.Y + ($targetY - $start.Y) * $eased)
        [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point $x, $y
        Start-Sleep -Milliseconds $stepDelay
    }
    # Guarantee the exact final position regardless of any rounding above.
    [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point ([int]$targetX), ([int]$targetY)
}

# ---------- input: SendKeys escaping ----------

function Escape-SendKeysText($text) {
    $special = '+^%~(){}[]'
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $text.ToCharArray()) {
        if ($special.IndexOf($ch) -ge 0) {
            $sb.Append('{').Append($ch).Append('}') | Out-Null
        } elseif ($ch -eq "`n") {
            $sb.Append('{ENTER}') | Out-Null
        } else {
            $sb.Append($ch) | Out-Null
        }
    }
    return $sb.ToString()
}

# `combo` like "Enter", "Tab", "Ctrl+S", "Alt+Tab", "Ctrl+Shift+Esc".
function Convert-ComboToSendKeys($combo) {
    $KEY_NAMES = @{
        enter = '{ENTER}'; tab = '{TAB}'; esc = '{ESC}'; escape = '{ESC}';
        backspace = '{BACKSPACE}'; delete = '{DELETE}'; del = '{DELETE}';
        home = '{HOME}'; end = '{END}'; pageup = '{PGUP}'; pagedown = '{PGDN}';
        up = '{UP}'; down = '{DOWN}'; left = '{LEFT}'; right = '{RIGHT}';
        space = ' '; f1='{F1}'; f2='{F2}'; f3='{F3}'; f4='{F4}'; f5='{F5}';
        f6='{F6}'; f7='{F7}'; f8='{F8}'; f9='{F9}'; f10='{F10}'; f11='{F11}'; f12='{F12}';
    }
    $parts = $combo -split '\+'
    $modPrefix = ''
    $mainKey = $parts[-1]
    foreach ($mod in $parts[0..($parts.Length - 2)]) {
        switch ($mod.ToLower()) {
            'ctrl' { $modPrefix += '^' }
            'control' { $modPrefix += '^' }
            'alt' { $modPrefix += '%' }
            'shift' { $modPrefix += '+' }
        }
    }
    $lowerMain = $mainKey.ToLower()
    if ($KEY_NAMES.ContainsKey($lowerMain)) {
        $mainPart = $KEY_NAMES[$lowerMain]
    } elseif ($mainKey.Length -eq 1) {
        $mainPart = Escape-SendKeysText $mainKey
    } else {
        $mainPart = '{' + $mainKey.ToUpper() + '}'
    }
    return $modPrefix + $mainPart
}

# ---------- dispatch ----------

function Invoke-Dispatch($req) {
    switch ($req.command) {
        'windows' {
            return @{ windows = @(Get-WindowList) }
        }
        'focus' {
            $handle = [IntPtr]::new([int64]$req.handle)
            [Native]::ShowWindow($handle, $SW_RESTORE) | Out-Null
            [Native]::SetForegroundWindow($handle) | Out-Null
            Start-Sleep -Milliseconds 150
            return @{ focused = $true }
        }
        'minimize_window' {
            # Tucks a window out of the way without touching the process
            # behind it — the "leave it running, just get it off screen" move.
            $handle = [IntPtr]::new([int64]$req.handle)
            [Native]::ShowWindow($handle, $SW_MINIMIZE) | Out-Null
            return @{ minimized = $true }
        }
        'restore_window' {
            # Brings a minimized window back to normal (not necessarily to
            # the foreground — that's what 'focus' is for). Separate from
            # 'focus' so the model can restore several windows into view
            # (e.g. for a side-by-side arrange) without each one also
            # stealing activation from the last.
            $handle = [IntPtr]::new([int64]$req.handle)
            [Native]::ShowWindow($handle, $SW_RESTORE) | Out-Null
            return @{ restored = $true }
        }
        'arrange_window' {
            # Position/size only — SWP_NOZORDER|SWP_NOACTIVATE means this
            # never reorders windows or steals focus, so arranging several
            # windows side-by-side doesn't fight itself over which is "on
            # top" between calls.
            $handle = [IntPtr]::new([int64]$req.handle)
            $flags = $SWP_NOZORDER -bor $SWP_NOACTIVATE
            [Native]::SetWindowPos($handle, [IntPtr]::Zero, [int]$req.x, [int]$req.y, [int]$req.width, [int]$req.height, $flags) | Out-Null
            return @{ arranged = $true }
        }
        'close_window' {
            # Requests a close the same way clicking the window's own X
            # button would (WM_CLOSE, posted asynchronously — never a
            # process kill). The target application decides for itself
            # whether to actually close (it may show its own "save changes?"
            # prompt) — this command only ever asks, it can't force it.
            $handle = [IntPtr]::new([int64]$req.handle)
            [Native]::PostMessage($handle, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
            return @{ closeRequested = $true }
        }
        'read_window' {
            return @{ elements = @(Read-WindowTree $req.handle) }
        }
        'screenshot' {
            $b64 = Take-Screenshot $req.handle $req.maxWidth
            return @{ base64 = $b64; mimeType = 'image/png' }
        }
        'click' {
            Move-CursorSmoothly $req.x $req.y
            Start-Sleep -Milliseconds 60
            [Native]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [IntPtr]::Zero)
            [Native]::mouse_event($MOUSEEVENTF_LEFTUP, 0, 0, 0, [IntPtr]::Zero)
            return @{ clicked = @{ x = $req.x; y = $req.y } }
        }
        'double_click' {
            Move-CursorSmoothly $req.x $req.y
            Start-Sleep -Milliseconds 60
            for ($i = 0; $i -lt 2; $i++) {
                [Native]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [IntPtr]::Zero)
                [Native]::mouse_event($MOUSEEVENTF_LEFTUP, 0, 0, 0, [IntPtr]::Zero)
                Start-Sleep -Milliseconds 40
            }
            return @{ doubleClicked = @{ x = $req.x; y = $req.y } }
        }
        'right_click' {
            Move-CursorSmoothly $req.x $req.y
            Start-Sleep -Milliseconds 60
            [Native]::mouse_event($MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, [IntPtr]::Zero)
            [Native]::mouse_event($MOUSEEVENTF_RIGHTUP, 0, 0, 0, [IntPtr]::Zero)
            return @{ rightClicked = @{ x = $req.x; y = $req.y } }
        }
        'move_cursor' {
            # Exposed on its own for a future "just point at this" action, and
            # for testing the glide independently of a click — not currently
            # called by session.js's own action set, which only ever moves the
            # cursor as part of a click.
            Move-CursorSmoothly $req.x $req.y
            return @{ moved = @{ x = $req.x; y = $req.y } }
        }
        'type' {
            $text = [string]$req.text
            if ($text.Length -gt 200) {
                $original = $null
                try { $original = [System.Windows.Forms.Clipboard]::GetText() } catch {}
                [System.Windows.Forms.Clipboard]::SetText($text)
                [System.Windows.Forms.SendKeys]::SendWait('^v')
                Start-Sleep -Milliseconds 300
                try {
                    if ($original) { [System.Windows.Forms.Clipboard]::SetText($original) }
                    else { [System.Windows.Forms.Clipboard]::Clear() }
                } catch {}
            } else {
                [System.Windows.Forms.SendKeys]::SendWait((Escape-SendKeysText $text))
            }
            return @{ typed = $true }
        }
        'key' {
            [System.Windows.Forms.SendKeys]::SendWait((Convert-ComboToSendKeys $req.combo))
            return @{ pressed = $req.combo }
        }
        'scroll' {
            $amount = if ($req.amount) { [int]$req.amount } else { 3 }
            $delta = 120 * $amount
            if ($req.direction -eq 'down') { $delta = -$delta }
            [Native]::mouse_event($MOUSEEVENTF_WHEEL, 0, 0, $delta, [IntPtr]::Zero)
            return @{ scrolled = $req.direction }
        }
        'cursor' {
            $pos = [System.Windows.Forms.Cursor]::Position
            return @{ x = $pos.X; y = $pos.Y }
        }
        'idle' {
            $lii = New-Object Native+LASTINPUTINFO
            $lii.cbSize = [uint32][System.Runtime.InteropServices.Marshal]::SizeOf($lii)
            [Native]::GetLastInputInfo([ref]$lii) | Out-Null
            $idleMs = [Environment]::TickCount - $lii.dwTime
            return @{ idleSeconds = [math]::Round($idleMs / 1000, 1) }
        }
        'processes' {
            # Every running process by name, not filtered to ones .NET
            # reports as having a "main window" — that heuristic is
            # unreliable for real GUI apps (confirmed live: explorer.exe's
            # actual File Explorer window, plainly visible via EnumWindows in
            # the 'windows' command above, was silently excluded here since
            # Get-Process's own MainWindowTitle missed it) and would also
            # miss any genuinely windowless background process (an
            # installer, a downloader) — exactly the kind of process a
            # "tell me when this finishes" monitor most needs to see `title`
            # is simply empty for a process with none, rather than the
            # process being dropped from the list entirely.
            $procs = Get-Process | Select-Object Id, ProcessName, MainWindowTitle
            return @{ processes = @($procs | ForEach-Object { @{ id = $_.Id; name = $_.ProcessName; title = $_.MainWindowTitle } }) }
        }
        default {
            throw "Unknown command: $($req.command)"
        }
    }
}

# ---------- main loop ----------
# One JSON object per line in, one JSON object per line out. `[Console]::In`
# (not `$input`/pipeline) is used so this reads stdin directly regardless of
# how Node spawned the process — matches how ps-bridge.js writes.

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try {
        $req = $line | ConvertFrom-Json
        $result = Invoke-Dispatch $req
        $response = @{ id = $req.id; ok = $true; result = $result }
    } catch {
        $response = @{ id = $req.id; ok = $false; error = $_.Exception.Message }
    }
    Write-Output ($response | ConvertTo-Json -Compress -Depth 8)
}
