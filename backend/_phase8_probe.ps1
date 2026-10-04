$root = 'C:\Build_AI\CR+CRM'
$app  = Join-Path $root 'backend\app'
$err  = @()

Write-Output '=== Phase-8 ground truth: what genuinely exists vs absent (no confabulation) ==='

# 1) OutboundMessage model: spec Phase 8 #4 says create only if absent
$out = Select-String -Path (Join-Path $app 'models\*.py') -Pattern 'class OutboundMessage' -ErrorAction SilentlyContinue
Write-Output ("- OutboundMessage: " + $(if($out){'EXISTS -> do not duplicate'}else{'ABSENT -> create (Phase 8 #4)'}))

# 2) sending service already writes an entity we should reuse (Phase 8 says reuse Sender/Provider architecture)
$sendPy = Join-Path $app 'services\sending.py'
Write-Output ("- services/sending.py: " + $(if(Test-Path $sendPy){'EXISTS'}else{'ABSENT'}))

# 3) outbound/sent audit constants already present? (reuse; Phase 8 #25)
$audit = Get-Content (Join-Path $app 'services\audit.py') -ErrorAction SilentlyContinue
$senders = $audit | Select-String -Pattern "SEND_QUEUED|SEND_STARTED|SEND_SUCCEEDED|SEND_RETRYING|SEND_FAILED|SEND_CANCELLED"
Write-Output ("- audit SEND_* constants: " + $(if($senders){'EXISTS'}
  else{'ABSENT -> add (Phase 8 #25): ' + (($audit | Select-String -Pattern '^[A-Z_]+ = ' | Select-Object -First 8) -join ' | ')}))

# 4) delivery queue framework: Phase 8 #17/#18 says REUSE existing queue; identify it
$qHits = Get-ChildItem -Recurse -File -Filter *.py $app -ErrorAction SilentlyContinue |
   Select-String -Pattern 'def enqueue_|^def enqueue|class .*Queue|class .*Worker|rpush|lpush|brpop' -ErrorAction SilentlyContinue
$qFiles = $qHits | ForEach-Object { Split-Path $_.Path -Leaf } | Sort-Object -Unique
Write-Output ("- queue/worker entrypoints (REUSE these; don't build a 2nd queue): " + ($qFiles -join ', '))

# 5) provider adapter + sender resolver present?  Phase 8 #7/#8/#9 must REUSE
$prov = Get-Content (Join-Path $app 'services\sender_health_engine\providers.py') -ErrorAction SilentlyContinue
Write-Output ("- build_provider_adapter: " + $(if($prov){'EXISTS (shared factory to reuse)'}else{'ABSENT'}))

# 6) existing test-send / verify infrastructure (Phase 8 #20 test send API must extend this)
$tsHits = Get-ChildItem -Recurse -File -Filter *.py $app -ErrorAction SilentlyContinue |
   Select-String -Pattern 'test-send|test_send|verify' -ErrorAction SilentlyContinue
Write-Output ("- existing test-send handles: " + (($tsHits | ForEach-Object { Split-Path $_.Path -Leaf } | Sort-Object -Unique | Select-Object -First 10) -join ', '))

Write-Output ''
Write-Output '=== messages/OutboundMessage storage — does alembic have an "outbound" migration already? ==='
$migs = Get-ChildItem (Join-Path $root 'backend\alembic\versions') -File -Filter *.py -ErrorAction SilentlyContinue
Write-Output ("- migration count: " + $migs.Count)
$migs | Where-Object { $_.Name -match 'outbound|message|send' } | ForEach-Object { $_.Name }
