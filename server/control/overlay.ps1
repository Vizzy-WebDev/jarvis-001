# Always-on-top "Jarvis is controlling your computer" bar + STOP button +
# global hotkey. Runs as its own process (see overlay-bridge.js) so it stays
# reachable even while Jarvis has focused a different window — the whole
# point of the safety spine is that Stop is never just "a button in a tab
# that isn't focused right now".
#
# Talks to Node in one direction only, by polling a small JSON status file
# (see overlay-bridge.js's writeStatus()) for what to show — no persistent
# IPC channel needed for that. Stop (button or hotkey) goes the other way:
# a plain HTTP POST straight to the server's own /api/control/stop, since
# PowerShell's Invoke-RestMethod makes that a one-liner and nothing else
# needs a live pipe to Node.
#
# Deliberately non-activating (WS_EX_NOACTIVATE) — see the NoActivateForm
# class below — so it can never steal keyboard focus from the app Jarvis
# (or the user) is actually working in, which would defeat the point of an
# overlay that's meant to sit passively on top.

# ---PARAM-BLOCK-START---
param(
  [int]$Port = 3000,
  [string]$StatusFile
)
# ---PARAM-BLOCK-END---

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Windows.Forms;

public class NoActivateForm : Form {
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get {
            const int WS_EX_NOACTIVATE = 0x08000000;
            const int WS_EX_TOPMOST = 0x00000008;
            CreateParams cp = base.CreateParams;
            cp.ExStyle |= WS_EX_NOACTIVATE | WS_EX_TOPMOST;
            return cp;
        }
    }
}
"@ -ReferencedAssemblies 'System.Windows.Forms', 'System.Drawing'

Add-Type @"
using System;
using System.Runtime.InteropServices;

public class KeyState {
    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int vKey);
}
"@

$form = New-Object NoActivateForm
$form.FormBorderStyle = 'None'
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.StartPosition = 'Manual'

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$barHeight = 40
$form.Width = $screen.Width
$form.Height = $barHeight
$form.Location = New-Object System.Drawing.Point($screen.X, $screen.Y)
$form.BackColor = [System.Drawing.Color]::FromArgb(200, 30, 30)

$label = New-Object System.Windows.Forms.Label
$label.Text = 'Jarvis is controlling your computer...'
$label.ForeColor = [System.Drawing.Color]::White
$label.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
$label.AutoSize = $false
$label.Width = $screen.Width - 200
$label.Height = $barHeight
$label.TextAlign = 'MiddleLeft'
$label.Location = New-Object System.Drawing.Point(16, 0)
$form.Controls.Add($label)

$stopBtn = New-Object System.Windows.Forms.Button
$stopBtn.Text = 'STOP  (Ctrl+Alt+J)'
$stopBtn.Width = 170
$stopBtn.Height = 30
$stopBtn.Location = New-Object System.Drawing.Point(($screen.Width - 186), 5)
$stopBtn.BackColor = [System.Drawing.Color]::White
$stopBtn.ForeColor = [System.Drawing.Color]::FromArgb(180, 0, 0)
$stopBtn.FlatStyle = 'Flat'
$stopBtn.Cursor = [System.Windows.Forms.Cursors]::Hand
$form.Controls.Add($stopBtn)

function Send-Stop {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/control/stop" -Method Post -TimeoutSec 3 | Out-Null
    } catch {
        # The server being briefly unreachable must never crash the overlay —
        # the user still sees the button do *something* (it disables itself
        # for a moment) even if this particular click didn't land.
    }
}

$stopBtn.Add_Click({ Send-Stop })

# ---- poll the status file for what to show, and for the shutdown signal ----
$pollTimer = New-Object System.Windows.Forms.Timer
$pollTimer.Interval = 400
$pollTimer.Add_Tick({
    try {
        if (-not (Test-Path $StatusFile)) { return }
        $raw = Get-Content -Path $StatusFile -Raw -ErrorAction Stop
        $status = $raw | ConvertFrom-Json
        if (-not $status.active) {
            $pollTimer.Stop()
            $form.Close()
            return
        }
        if ($status.step) { $label.Text = $status.step }
    } catch {
        # A half-written status file (Node mid-write) just skips this tick —
        # writeStatus() on the Node side writes atomically, but a transient
        # read failure here is harmless either way.
    }
})
$pollTimer.Start()

# ---- global hotkey: Ctrl+Alt+J, polled rather than registered, so this
# works even though the form is deliberately non-activating (RegisterHotKey
# delivers WM_HOTKEY to the owning window, which is easy to miss guarantees
# on for a no-activate window; polling has no such caveat). ----
$VK_J = 0x4A
$VK_CONTROL = 0x11
$VK_MENU = 0x12 # Alt
$hotkeyTimer = New-Object System.Windows.Forms.Timer
$hotkeyTimer.Interval = 150
$lastHotkeyState = $false
$hotkeyTimer.Add_Tick({
    $ctrl = ([KeyState]::GetAsyncKeyState($VK_CONTROL) -band 0x8000) -ne 0
    $alt = ([KeyState]::GetAsyncKeyState($VK_MENU) -band 0x8000) -ne 0
    $j = ([KeyState]::GetAsyncKeyState($VK_J) -band 0x8000) -ne 0
    $down = $ctrl -and $alt -and $j
    if ($down -and -not $lastHotkeyState) {
        Send-Stop
    }
    $lastHotkeyState = $down
})
$hotkeyTimer.Start()

[System.Windows.Forms.Application]::Run($form)
