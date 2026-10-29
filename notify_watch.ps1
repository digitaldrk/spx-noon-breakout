param([switch]$Test)
$state = Join-Path $PSScriptRoot 'state'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Toast($t, $m) {
    $x = New-Object Windows.Data.Xml.Dom.XmlDocument
    $title = [Security.SecurityElement]::Escape($t)
    $body = [Security.SecurityElement]::Escape($m)
    $x.LoadXml(@"
<toast scenario="reminder">
  <visual>
    <binding template="ToastGeneric">
      <text>$title</text>
      <text>$body</text>
    </binding>
  </visual>
  <actions>
    <action content="Got it" arguments="ok" activationType="foreground"/>
  </actions>
  <audio src="ms-winsoundevent:Notification.Looping.Alarm"/>
</toast>
"@)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
        [Windows.UI.Notifications.ToastNotification]::new($x))
}

if ($Test) {
    Toast 'High Noon Boi' 'Test pop-up: alerts are wired in. :face_with_cowboy_hat:'
    return
}

$seen = @{}
while ($true) {
    $f = Join-Path $state ("bot_{0}.log" -f (Get-Date -f 'yyyy-MM-dd'))
    if (Test-Path $f) {
        $i = 0
        foreach ($l in Get-Content $f) {
            $i++
            $k = "$f#$i"
            if (-not $seen[$k]) {
                $seen[$k] = 1
                if ($script:primed -and $l -notmatch 'no breakout') {
                    Toast 'High Noon Boi' ($l -replace '^\S+ \S+ \w+ ', '')
                }
            }
        }
    }
    $script:primed = $true
    Start-Sleep 5
}
