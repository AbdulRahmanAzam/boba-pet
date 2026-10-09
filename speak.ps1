# Reads UTF-8 text from stdin and speaks it with Windows text-to-speech.
param([string]$Voice = '', [int]$Rate = 1)
Add-Type -AssemblyName System.Speech

$reader = New-Object IO.StreamReader([Console]::OpenStandardInput(), [Text.Encoding]::UTF8)
$text = $reader.ReadToEnd()
if (-not $text.Trim()) { exit 0 }

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($Voice) { try { $synth.SelectVoice($Voice) } catch { } }
$synth.Rate = $Rate
$synth.Speak($text)
$synth.Dispose()
