# Small always-on-top circular badge, top-right corner: "Jarvis can currently
# see your screen." Separate on purpose from overlay.ps1's red control bar —
# that one means "Jarvis is actively operating your mouse/keyboard" (and
# already implies it can see the screen too); this one means "Jarvis is only
# looking," for look_at_screen.js's one-off glance and a screen_looks_like
# monitor's ongoing visual watch. See observation-bridge.js's header comment
# for the full division of labor.
#
# Same non-activating/topmost/status-file-polling shape as overlay.ps1 (see
# that file's own comments for why each piece is built the way it is) — a
# small circle instead of a full-width bar, no text label (nothing to say
# beyond "on" or "off"), and a click anywhere on it posts to the server's
# /api/observation/stop instead of /api/control/stop. That route stops every
# currently-active visual watch; if nothing is actually watching right now
# (e.g. this is showing for a few-second look_at_screen glance), the click is
# a harmless no-op — the badge doesn't need to know which case it's in.

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

$form = New-Object NoActivateForm
$form.FormBorderStyle = 'None'
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.StartPosition = 'Manual'

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$size = 18
$margin = 14
$form.Width = $size
$form.Height = $size
$form.Location = New-Object System.Drawing.Point(($screen.X + $screen.Width - $size - $margin), ($screen.Y + $margin))
$form.BackColor = [System.Drawing.Color]::FromArgb(45, 110, 230)

# Clip the square form to a circle — a plain BackColor square with a
# GraphicsPath region is the simplest way to get a round window without a
# transparency key fighting the "always on top of everything" requirement.
$path = New-Object System.Drawing.Drawing2D.GraphicsPath
$path.AddEllipse(0, 0, $size, $size)
$form.Region = New-Object System.Drawing.Region($path)

$tip = New-Object System.Windows.Forms.ToolTip
$tip.SetToolTip($form, "Jarvis can see your screen - click to stop")

function Send-Stop {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/observation/stop" -Method Post -TimeoutSec 3 | Out-Null
    } catch {
        # Server briefly unreachable — the badge itself keeps polling the
        # status file regardless, same "harmless if this click doesn't land"
        # reasoning as overlay.ps1's Send-Stop.
    }
}

$form.Add_Click({ Send-Stop })
$form.Cursor = [System.Windows.Forms.Cursors]::Hand

# ---- poll the status file for on/off ----
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
        }
    } catch {
        # A half-written status file (Node mid-write) just skips this tick —
        # writeStatusFile() writes atomically, so this is always transient.
    }
})
$pollTimer.Start()

[System.Windows.Forms.Application]::Run($form)
