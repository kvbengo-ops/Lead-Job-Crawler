param(
    [Parameter(Mandatory=$true)][string]$Title,
    [Parameter(Mandatory=$true)][string]$Body,
    [string]$Url = "http://127.0.0.1:8100"
)

# Windows PowerShell 5.1 WinRT toast; no external module required.
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast launch='$Url'><visual><binding template='ToastGeneric'><text>$([System.Security.SecurityElement]::Escape($Title))</text><text>$([System.Security.SecurityElement]::Escape($Body))</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("JobCrawler").Show($toast)
