$remote = "/tmp/test"
$s = "cd $remote && sed -i 's/\r$//' scripts/*.sh 2>/dev/null; true"
Write-Host "RESULT:" $s
