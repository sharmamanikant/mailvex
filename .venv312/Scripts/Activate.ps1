<#
.Synopsis
Activate a Python virtual environment for the current PowerShell session.

.Description
Pushes the python executable for a virtual environment to the front of the
$Env:PATH environment variable and sets the prompt to signify that you are
in a Python virtual environment. Makes use of the command line switches as
well as the `pyvenv.cfg` file values present in the virtual environment.

.Parameter VenvDir
Path to the directory that contains the virtual environment to activate. The
default value for this is the parent of the directory that the Activate.ps1
script is located within.

.Parameter Prompt
The prompt prefix to display when this virtual environment is activated. By
default, this prompt is the name of the virtual environment folder (VenvDir)
surrounded by parentheses and followed by a single space (ie. '(.venv) ').

.Example
Activate.ps1
Activates the Python virtual environment that contains the Activate.ps1 script.

.Example
Activate.ps1 -Verbose
Activates the Python virtual environment that contains the Activate.ps1 script,
and shows extra information about the activation as it executes.

.Example
Activate.ps1 -VenvDir C:\Users\MyUser\Common\.venv
Activates the Python virtual environment located in the specified location.

.Example
Activate.ps1 -Prompt "MyPython"
Activates the Python virtual environment that contains the Activate.ps1 script,
and prefixes the current prompt with the specified string (surrounded in
parentheses) while the virtual environment is active.

.Notes
On Windows, it may be required to enable this Activate.ps1 script by setting the
execution policy for the user. You can do this by issuing the following PowerShell
command:

PS C:\> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

For more information on Execution Policies: 
https://go.microsoft.com/fwlink/?LinkID=135170

#>
Param(
    [Parameter(Mandatory = $false)]
    [String]
    $VenvDir,
    [Parameter(Mandatory = $false)]
    [String]
    $Prompt
)

<# Function declarations --------------------------------------------------- #>

<#
.Synopsis
Remove all shell session elements added by the Activate script, including the
addition of the virtual environment's Python executable from the beginning of
the PATH variable.

.Parameter NonDestructive
If present, do not remove this function from the global namespace for the
session.

#>
function global:deactivate ([switch]$NonDestructive) {
    # Revert to original values

    # The prior prompt:
    if (Test-Path -Path Function:_OLD_VIRTUAL_PROMPT) {
        Copy-Item -Path Function:_OLD_VIRTUAL_PROMPT -Destination Function:prompt
        Remove-Item -Path Function:_OLD_VIRTUAL_PROMPT
    }

    # The prior PYTHONHOME:
    if (Test-Path -Path Env:_OLD_VIRTUAL_PYTHONHOME) {
        Copy-Item -Path Env:_OLD_VIRTUAL_PYTHONHOME -Destination Env:PYTHONHOME
        Remove-Item -Path Env:_OLD_VIRTUAL_PYTHONHOME
    }

    # The prior PATH:
    if (Test-Path -Path Env:_OLD_VIRTUAL_PATH) {
        Copy-Item -Path Env:_OLD_VIRTUAL_PATH -Destination Env:PATH
        Remove-Item -Path Env:_OLD_VIRTUAL_PATH
    }

    # Just remove the VIRTUAL_ENV altogether:
    if (Test-Path -Path Env:VIRTUAL_ENV) {
        Remove-Item -Path env:VIRTUAL_ENV
    }

    # Just remove VIRTUAL_ENV_PROMPT altogether.
    if (Test-Path -Path Env:VIRTUAL_ENV_PROMPT) {
        Remove-Item -Path env:VIRTUAL_ENV_PROMPT
    }

    # Just remove the _PYTHON_VENV_PROMPT_PREFIX altogether:
    if (Get-Variable -Name "_PYTHON_VENV_PROMPT_PREFIX" -ErrorAction SilentlyContinue) {
        Remove-Variable -Name _PYTHON_VENV_PROMPT_PREFIX -Scope Global -Force
    }

    # Leave deactivate function in the global namespace if requested:
    if (-not $NonDestructive) {
        Remove-Item -Path function:deactivate
    }
}

<#
.Description
Get-PyVenvConfig parses the values from the pyvenv.cfg file located in the
given folder, and returns them in a map.

For each line in the pyvenv.cfg file, if that line can be parsed into exactly
two strings separated by `=` (with any amount of whitespace surrounding the =)
then it is considered a `key = value` line. The left hand string is the key,
the right hand is the value.

If the value starts with a `'` or a `"` then the first and last character is
stripped from the value before being captured.

.Parameter ConfigDir
Path to the directory that contains the `pyvenv.cfg` file.
#>
function Get-PyVenvConfig(
    [String]
    $ConfigDir
) {
    Write-Verbose "Given ConfigDir=$ConfigDir, obtain values in pyvenv.cfg"

    # Ensure the file exists, and issue a warning if it doesn't (but still allow the function to continue).
    $pyvenvConfigPath = Join-Path -Resolve -Path $ConfigDir -ChildPath 'pyvenv.cfg' -ErrorAction Continue

    # An empty map will be returned if no config file is found.
    $pyvenvConfig = @{ }

    if ($pyvenvConfigPath) {

        Write-Verbose "File exists, parse `key = value` lines"
        $pyvenvConfigContent = Get-Content -Path $pyvenvConfigPath

        $pyvenvConfigContent | ForEach-Object {
            $keyval = $PSItem -split "\s*=\s*", 2
            if ($keyval[0] -and $keyval[1]) {
                $val = $keyval[1]

                # Remove extraneous quotations around a string value.
                if ("'""".Contains($val.Substring(0, 1))) {
                    $val = $val.Substring(1, $val.Length - 2)
                }

                $pyvenvConfig[$keyval[0]] = $val
                Write-Verbose "Adding Key: '$($keyval[0])'='$val'"
            }
        }
    }
    return $pyvenvConfig
}


<# Begin Activate script --------------------------------------------------- #>

# Determine the containing directory of this script
$VenvExecPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvExecDir = Get-Item -Path $VenvExecPath

Write-Verbose "Activation script is located in path: '$VenvExecPath'"
Write-Verbose "VenvExecDir Fullname: '$($VenvExecDir.FullName)"
Write-Verbose "VenvExecDir Name: '$($VenvExecDir.Name)"

# Set values required in priority: CmdLine, ConfigFile, Default
# First, get the location of the virtual environment, it might not be
# VenvExecDir if specified on the command line.
if ($VenvDir) {
    Write-Verbose "VenvDir given as parameter, using '$VenvDir' to determine values"
}
else {
    Write-Verbose "VenvDir not given as a parameter, using parent directory name as VenvDir."
    $VenvDir = $VenvExecDir.Parent.FullName.TrimEnd("\\/")
    Write-Verbose "VenvDir=$VenvDir"
}

# Next, read the `pyvenv.cfg` file to determine any required value such
# as `prompt`.
$pyvenvCfg = Get-PyVenvConfig -ConfigDir $VenvDir

# Next, set the prompt from the command line, or the config file, or
# just use the name of the virtual environment folder.
if ($Prompt) {
    Write-Verbose "Prompt specified as argument, using '$Prompt'"
}
else {
    Write-Verbose "Prompt not specified as argument to script, checking pyvenv.cfg value"
    if ($pyvenvCfg -and $pyvenvCfg['prompt']) {
        Write-Verbose "  Setting based on value in pyvenv.cfg='$($pyvenvCfg['prompt'])'"
        $Prompt = $pyvenvCfg['prompt'];
    }
    else {
        Write-Verbose "  Setting prompt based on parent's directory's name. (Is the directory name passed to venv module when creating the virtual environment)"
        Write-Verbose "  Got leaf-name of $VenvDir='$(Split-Path -Path $venvDir -Leaf)'"
        $Prompt = Split-Path -Path $venvDir -Leaf
    }
}

Write-Verbose "Prompt = '$Prompt'"
Write-Verbose "VenvDir='$VenvDir'"

# Deactivate any currently active virtual environment, but leave the
# deactivate function in place.
deactivate -nondestructive

# Now set the environment variable VIRTUAL_ENV, used by many tools to determine
# that there is an activated venv.
$env:VIRTUAL_ENV = $VenvDir

$env:VIRTUAL_ENV_PROMPT = $Prompt

if (-not $Env:VIRTUAL_ENV_DISABLE_PROMPT) {

    Write-Verbose "Setting prompt to '$Prompt'"

    # Set the prompt to include the env name
    # Make sure _OLD_VIRTUAL_PROMPT is global
    function global:_OLD_VIRTUAL_PROMPT { "" }
    Copy-Item -Path function:prompt -Destination function:_OLD_VIRTUAL_PROMPT
    New-Variable -Name _PYTHON_VENV_PROMPT_PREFIX -Description "Python virtual environment prompt prefix" -Scope Global -Option ReadOnly -Visibility Public -Value $Prompt

    function global:prompt {
        Write-Host -NoNewline -ForegroundColor Green "($_PYTHON_VENV_PROMPT_PREFIX) "
        _OLD_VIRTUAL_PROMPT
    }
}

# Clear PYTHONHOME
if (Test-Path -Path Env:PYTHONHOME) {
    Copy-Item -Path Env:PYTHONHOME -Destination Env:_OLD_VIRTUAL_PYTHONHOME
    Remove-Item -Path Env:PYTHONHOME
}

# Add the venv to the PATH
Copy-Item -Path Env:PATH -Destination Env:_OLD_VIRTUAL_PATH
$Env:PATH = "$VenvExecDir$([System.IO.Path]::PathSeparator)$Env:PATH"

# SIG # Begin signature block
# MII28gYJKoZIhvcNAQcCoII24zCCNt8CAQExDzANBglghkgBZQMEAgEFADB5Bgor
# BgEEAYI3AgEEoGswaTA0BgorBgEEAYI3AgEeMCYCAwEAAAQQH8w7YFlLCE63JNLG
# KX7zUQIBAAIBAAIBAAIBAAIBADAxMA0GCWCGSAFlAwQCAQUABCBALKwKRFIhr2RY
# IW/WJLd9pc8a9sj/IoThKU92fTfKsKCCG1wwggXMMIIDtKADAgECAhBUmNLR1FsZ
# lUgTecgRwIeZMA0GCSqGSIb3DQEBDAUAMHcxCzAJBgNVBAYTAlVTMR4wHAYDVQQK
# ExVNaWNyb3NvZnQgQ29ycG9yYXRpb24xSDBGBgNVBAMTP01pY3Jvc29mdCBJZGVu
# dGl0eSBWZXJpZmljYXRpb24gUm9vdCBDZXJ0aWZpY2F0ZSBBdXRob3JpdHkgMjAy
# MDAeFw0yMDA0MTYxODM2MTZaFw00NTA0MTYxODQ0NDBaMHcxCzAJBgNVBAYTAlVT
# MR4wHAYDVQQKExVNaWNyb3NvZnQgQ29ycG9yYXRpb24xSDBGBgNVBAMTP01pY3Jv
# c29mdCBJZGVudGl0eSBWZXJpZmljYXRpb24gUm9vdCBDZXJ0aWZpY2F0ZSBBdXRo
# b3JpdHkgMjAyMDCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBALORKgeD
# Bmf9np3gx8C3pOZCBH8Ppttf+9Va10Wg+3cL8IDzpm1aTXlT2KCGhFdFIMeiVPvH
# or+Kx24186IVxC9O40qFlkkN/76Z2BT2vCcH7kKbK/ULkgbk/WkTZaiRcvKYhOuD
# PQ7k13ESSCHLDe32R0m3m/nJxxe2hE//uKya13NnSYXjhr03QNAlhtTetcJtYmrV
# qXi8LW9J+eVsFBT9FMfTZRY33stuvF4pjf1imxUs1gXmuYkyM6Nix9fWUmcIxC70
# ViueC4fM7Ke0pqrrBc0ZV6U6CwQnHJFnni1iLS8evtrAIMsEGcoz+4m+mOJyoHI1
# vnnhnINv5G0Xb5DzPQCGdTiO0OBJmrvb0/gwytVXiGhNctO/bX9x2P29Da6SZEi3
# W295JrXNm5UhhNHvDzI9e1eM80UHTHzgXhgONXaLbZ7LNnSrBfjgc10yVpRnlyUK
# xjU9lJfnwUSLgP3B+PR0GeUw9gb7IVc+BhyLaxWGJ0l7gpPKWeh1R+g/OPTHU3mg
# trTiXFHvvV84wRPmeAyVWi7FQFkozA8kwOy6CXcjmTimthzax7ogttc32H83rwjj
# O3HbbnMbfZlysOSGM1l0tRYAe1BtxoYT2v3EOYI9JACaYNq6lMAFUSw0rFCZE4e7
# swWAsk0wAly4JoNdtGNz764jlU9gKL431VulAgMBAAGjVDBSMA4GA1UdDwEB/wQE
# AwIBhjAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQWBBTIftJqhSobyhmYBAcnz1AQ
# T2ioojAQBgkrBgEEAYI3FQEEAwIBADANBgkqhkiG9w0BAQwFAAOCAgEAr2rd5hnn
# LZRDGU7L6VCVZKUDkQKL4jaAOxWiUsIWGbZqWl10QzD0m/9gdAmxIR6QFm3FJI9c
# Zohj9E/MffISTEAQiwGf2qnIrvKVG8+dBetJPnSgaFvlVixlHIJ+U9pW2UYXeZJF
# xBA2CFIpF8svpvJ+1Gkkih6PsHMNzBxKq7Kq7aeRYwFkIqgyuH4yKLNncy2RtNwx
# AQv3Rwqm8ddK7VZgxCwIo3tAsLx0J1KH1r6I3TeKiW5niB31yV2g/rarOoDXGpc8
# FzYiQR6sTdWD5jw4vU8w6VSp07YEwzJ2YbuwGMUrGLPAgNW3lbBeUU0i/OxYqujY
# lLSlLu2S3ucYfCFX3VVj979tzR/SpncocMfiWzpbCNJbTsgAlrPhgzavhgplXHT2
# 6ux6anSg8Evu75SjrFDyh+3XOjCDyft9V77l4/hByuVkrrOj7FjshZrM77nq81YY
# uVxzmq/FdxeDWds3GhhyVKVB0rYjdaNDmuV3fJZ5t0GNv+zcgKCf0Xd1WF81E+Al
# GmcLfc4l+gcK5GEh2NQc5QfGNpn0ltDGFf5Ozdeui53bFv0ExpK91IjmqaOqu/dk
# ODtfzAzQNb50GQOmxapMomE2gj4d8yu8l13bS3g7LfU772Aj6PXsCyM2la+YZr9T
# 03u4aUoqlmZpxJTG9F9urJh4iIAGXKKy7aIwgga6MIIEoqADAgECAhMzAAQ8jxuX
# RWo6DyI/AAAABDyPMA0GCSqGSIb3DQEBDAUAMFoxCzAJBgNVBAYTAlVTMR4wHAYD
# VQQKExVNaWNyb3NvZnQgQ29ycG9yYXRpb24xKzApBgNVBAMTIk1pY3Jvc29mdCBJ
# RCBWZXJpZmllZCBDUyBBT0MgQ0EgMDQwHhcNMjYwODAzMTEwMzM2WhcNMjYwODA2
# MTEwMzM2WjB8MQswCQYDVQQGEwJVUzEPMA0GA1UECBMGT3JlZ29uMRIwEAYDVQQH
# EwlCZWF2ZXJ0b24xIzAhBgNVBAoTGlB5dGhvbiBTb2Z0d2FyZSBGb3VuZGF0aW9u
# MSMwIQYDVQQDExpQeXRob24gU29mdHdhcmUgRm91bmRhdGlvbjCCAaIwDQYJKoZI
# hvcNAQEBBQADggGPADCCAYoCggGBAIgpAEgsN+/t4fTu8lyBbSgVB4sXK6ETr7Yt
# uA77F/SCyTgnWxPVtfX1niIwSzTEnx7QTUP98yoOw8LDJu+mzxDAHJLLE9LZLBW1
# gqd7poWT4Kqp0rlqqNv6lUvmsOOK3kGudy2lS2gPYbPRwUv2wFaPiZeMhcE0bl2a
# 7ygCPiG0wJBXikkcSNxItYeIQbypya0vDn7TOoWfqZs1vDYy+z3dwOKxRLKo16ne
# qjT/ZfsHoCCGDNXw668nGtdmjNeJYbZC4UaHYq+wqSs0H3B7pmlfcNnNxAQr+7pw
# d35i0YqE2+lTWPKd4ptL7uixLP3PMBl1wzJpJ1pF0lT7YeMqdvQfnSV0Ld359cE7
# R3JQow4PaiCD+3xhToC+3YRQh+YLzKrGbwNP3lFsfujksXJ6d05SrFVlUno7Gqfp
# 5QE4P3XWZmZYaCt2EeUCx4nxr4E+Y0cY7OLmChKpelS1BW/W0Mhb52MlwQAYsBXJ
# 4RTGBQ0Kx76/sztTusVUe0mJ8j5g+wIDAQABo4IB1TCCAdEwDAYDVR0TAQH/BAIw
# ADAOBgNVHQ8BAf8EBAMCB4AwPAYDVR0lBDUwMwYKKwYBBAGCN2EBAAYIKwYBBQUH
# AwMGGysGAQQBgjdhgqKNuwqmkohkgZH0oEWCk/3hbzAdBgNVHQ4EFgQULY9bDd/N
# Twy1S5NTN3cmSnU0o+EwHwYDVR0jBBgwFoAUayVB3vtrfP0YgAotf492XapzPbgw
# ZwYDVR0fBGAwXjBcoFqgWIZWaHR0cDovL3d3dy5taWNyb3NvZnQuY29tL3BraW9w
# cy9jcmwvTWljcm9zb2Z0JTIwSUQlMjBWZXJpZmllZCUyMENTJTIwQU9DJTIwQ0El
# MjAwNC5jcmwwdAYIKwYBBQUHAQEEaDBmMGQGCCsGAQUFBzAChlhodHRwOi8vd3d3
# Lm1pY3Jvc29mdC5jb20vcGtpb3BzL2NlcnRzL01pY3Jvc29mdCUyMElEJTIwVmVy
# aWZpZWQlMjBDUyUyMEFPQyUyMENBJTIwMDQuY3J0MFQGA1UdIARNMEswSQYEVR0g
# ADBBMD8GCCsGAQUFBwIBFjNodHRwOi8vd3d3Lm1pY3Jvc29mdC5jb20vcGtpb3Bz
# L0RvY3MvUmVwb3NpdG9yeS5odG0wDQYJKoZIhvcNAQEMBQADggIBAEtWfA6rjYZi
# VZ8kaPiffdPW1RZBjmxvP972vI1osTrfQeEUQco+lIHrzFgL20meofmyI4RxidJI
# Z/Wd6VRbkASSj6xxXlQzo/S4BHj57ZtqVlbANvvYdGjOUkaiyfhwMS3y2PWrlNep
# bJZ/bpAX0CKpYgseB/NDYAJ2PHgiYXgwz82zPRLqBxbqqtewBVS8BqJ7H3WinUY1
# AcOxht+gF4MEL3vABTlM4hQEQULcGgYNQzte5SuAJBqMCYThRnaFFYAvIaI91ViO
# Z3b9RzF3hHE6RXFPjhRq2Cpo9OHLkc5wWXXBJwdIOrZjIpHfAVSIwy7J9h19Lumd
# fEqRZU/9k2r08WDxX5yBqlkqWUgGYP9hPn2HcZ7cvEqZF5KLLXKkGU5pKeBqFacI
# G2wF2cHJTomVxm21IXmnmZHQXSA9NEluDSeFifLOChaFt9HYZDrjdZEW3y89CfIY
# B1sybpc1wBICJxEvpf4oUaOLDE87eD61aHkFtA/7isEnsdQF7tupBsaSyaRr5GB8
# ZPfpMDNIHEicgq+yck9FZscvgOK0aF5FOITklVpG/+1kyz0/ORPC9iuYS9Uxv44N
# jE1+hw9PzKWJRVmsKQKt4RmdTonvSD+3kygA9UIVuxcbJeAtalyBpCnnsjrAy6l0
# 3LQ1LbGrw/hxL60AAll81Tust+LMzSlmMIIHKDCCBRCgAwIBAgITMwAAABYxko2S
# AmV7mgAAAAAAFjANBgkqhkiG9w0BAQwFADBjMQswCQYDVQQGEwJVUzEeMBwGA1UE
# ChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMTQwMgYDVQQDEytNaWNyb3NvZnQgSUQg
# VmVyaWZpZWQgQ29kZSBTaWduaW5nIFBDQSAyMDIxMB4XDTI2MDMyNjE4MTEyOVoX
# DTMxMDMyNjE4MTEyOVowWjELMAkGA1UEBhMCVVMxHjAcBgNVBAoTFU1pY3Jvc29m
# dCBDb3Jwb3JhdGlvbjErMCkGA1UEAxMiTWljcm9zb2Z0IElEIFZlcmlmaWVkIENT
# IEFPQyBDQSAwNDCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAMpV+sjb
# 6Akwz/RtDk5Uo1284BLMttRQy9e5/5WXtga6h89pkdWjeAwcXmKdP4YxKkhx8hn2
# q1dTVQbryvLy81tC04vg2bfSJ9emojX4HKIBYs7VhPRafMbtc866hN55aw1m/kWa
# PEKxF4Fm/LPLMLJdlu7URB8nFZMfh5tTC0CJb2uox/14OP/BiGIR8214lXdkV6Js
# PbO0Iev0mEV133tducIeBChMipzTZfnGVEq1QYFr7460cEGOIn+7AGfNOSq7gWOl
# mNB4m2uZ1r66vUJPaN+VYgH/Kmfu7tX229b3Alsli38fYS0nQY41bElntMS7yNY+
# Kd047eXM8/tS3NL+ZUNX3ge5xZqW2aytrrNIbwGgQnzsgzxBJvu9+b87jUWFiRS/
# z1YiPkRLY7iTHKIxJ973kIyK8K5itE/aEq/Ht6A8ytaAMMGTEwuCspk72FE1Qyby
# +TfDlv1KiAc7IlWHHIWbxoVd8jGCoMXhLSDhuFuGfOWOZKUIuW6YxlxcUYOOkXa0
# 2gC7dUYUZi0e1NI1Uq9mmAHdvjKdqgORs9/5aDjnbeO3hWd5qwGBmELWytkjY0Kc
# IEF+CMurTimoQoBYSiHJbYrb0pKSQe+3lVoTVpzdi36jKI7MxLnu1fBAMksa8uD8
# 5cU7RU29zMOd18h9dgTEH9pvY+4xLB3lUgE7AgMBAAGjggHcMIIB2DAOBgNVHQ8B
# Af8EBAMCAYYwEAYJKwYBBAGCNxUBBAMCAQAwHQYDVR0OBBYEFGslQd77a3z9GIAK
# LX+Pdl2qcz24MFQGA1UdIARNMEswSQYEVR0gADBBMD8GCCsGAQUFBwIBFjNodHRw
# Oi8vd3d3Lm1pY3Jvc29mdC5jb20vcGtpb3BzL0RvY3MvUmVwb3NpdG9yeS5odG0w
# GQYJKwYBBAGCNxQCBAweCgBTAHUAYgBDAEEwEgYDVR0TAQH/BAgwBgEB/wIBADAf
# BgNVHSMEGDAWgBTZQSmwDw9jbO9p1/XNKZ6kSGow5jBwBgNVHR8EaTBnMGWgY6Bh
# hl9odHRwOi8vd3d3Lm1pY3Jvc29mdC5jb20vcGtpb3BzL2NybC9NaWNyb3NvZnQl
# MjBJRCUyMFZlcmlmaWVkJTIwQ29kZSUyMFNpZ25pbmclMjBQQ0ElMjAyMDIxLmNy
# bDB9BggrBgEFBQcBAQRxMG8wbQYIKwYBBQUHMAKGYWh0dHA6Ly93d3cubWljcm9z
# b2Z0LmNvbS9wa2lvcHMvY2VydHMvTWljcm9zb2Z0JTIwSUQlMjBWZXJpZmllZCUy
# MENvZGUlMjBTaWduaW5nJTIwUENBJTIwMjAyMS5jcnQwDQYJKoZIhvcNAQEMBQAD
# ggIBAAbVUF5UdNVEGWOVxkPciIzHA/IyNDIs1oSdW7mY4ObaFEEl8fwawEsBOasc
# BzwXjuOaTkKelQ+IiC4JvDpn8pWPgOyiKrbbw1Iswt7AV8YyuLu7A0YshILlxyny
# 9R0AMMQixLZ0T5Aj07CQOm/de5QPt36ECBwtVww6XUCx8Rm2xl6eEa9JhSub8W4u
# rQGoQYv0Zczlz4ej2ryj5Wf9L4ZZA3bL6CRE7XmzSQjTmdSmr904PiuL2uBWzq2K
# kR3RhmoaAP4Jk2JplasNM5Bs0+dx3YX2o6xRrbSaJiu6hPk/AkBoj5BCJMTZkl4w
# k6Q6nOFNSCpUxnBmJ0RRkoKq51p5ADTxbCeRAx8rIfNpTyPjxQtxTiFTC8yy/t9K
# 6s570YB1FAI+8XxZxIrmgMd7xVUkzi2/oooKb3UeovH0lqYGBEjpHZJ9jee7boQb
# Oe7SsKD/vr3PzFabg9VwLZlovpWUpWoWnU2w3xiozJ/m35tsZVvT2egUpwkb9TDI
# aXFGZAGV94FRDHn/K2XNnOwSeGskX19MB+N7yPc51fmUSd49MVAtGW/NkUGpRech
# 5Aq/d8dCML2bZ+j9nTUMgj+qnd3wuEZVRbQEIbTh9HAck0fyKqoIb+qh8y/6TKbY
# DEEOcfM2BGMvrQk4WIGCK6u8uFhA2EH3kpaiMUDqyFCeCCQxMIIHnjCCBYagAwIB
# AgITMwAAAAeHozSje6WOHAAAAAAABzANBgkqhkiG9w0BAQwFADB3MQswCQYDVQQG
# EwJVUzEeMBwGA1UEChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMUgwRgYDVQQDEz9N
# aWNyb3NvZnQgSWRlbnRpdHkgVmVyaWZpY2F0aW9uIFJvb3QgQ2VydGlmaWNhdGUg
# QXV0aG9yaXR5IDIwMjAwHhcNMjEwNDAxMjAwNTIwWhcNMzYwNDAxMjAxNTIwWjBj
# MQswCQYDVQQGEwJVUzEeMBwGA1UEChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMTQw
# MgYDVQQDEytNaWNyb3NvZnQgSUQgVmVyaWZpZWQgQ29kZSBTaWduaW5nIFBDQSAy
# MDIxMIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAsvDArxmIKOLdVHpM
# SWxpCFUJtFL/ekr4weslKPdnF3cpTeuV8veqtmKVgok2rO0D05BpyvUDCg1wdsoE
# tuxACEGcgHfjPF/nZsOkg7c0mV8hpMT/GvB4uhDvWXMIeQPsDgCzUGzTvoi76YDp
# xDOxhgf8JuXWJzBDoLrmtThX01CE1TCCvH2sZD/+Hz3RDwl2MsvDSdX5rJDYVuR3
# bjaj2QfzZFmwfccTKqMAHlrz4B7ac8g9zyxlTpkTuJGtFnLBGasoOnn5NyYlf0xF
# 9/bjVRo4Gzg2Yc7KR7yhTVNiuTGH5h4eB9ajm1OCShIyhrKqgOkc4smz6obxO+Hx
# KeJ9bYmPf6KLXVNLz8UaeARo0BatvJ82sLr2gqlFBdj1sYfqOf00Qm/3B4XGFPDK
# /H04kteZEZsBRc3VT2d/iVd7OTLpSH9yCORV3oIZQB/Qr4nD4YT/lWkhVtw2v2s0
# TnRJubL/hFMIQa86rcaGMhNsJrhysLNNMeBhiMezU1s5zpusf54qlYu2v5sZ5zL0
# KvBDLHtL8F9gn6jOy3v7Jm0bbBHjrW5yQW7S36ALAt03QDpwW1JG1Hxu/FUXJbBO
# 2AwwVG4Fre+ZQ5Od8ouwt59FpBxVOBGfN4vN2m3fZx1gqn52GvaiBz6ozorgIEjn
# +PhUXILhAV5Q/ZgCJ0u2+ldFGjcCAwEAAaOCAjUwggIxMA4GA1UdDwEB/wQEAwIB
# hjAQBgkrBgEEAYI3FQEEAwIBADAdBgNVHQ4EFgQU2UEpsA8PY2zvadf1zSmepEhq
# MOYwVAYDVR0gBE0wSzBJBgRVHSAAMEEwPwYIKwYBBQUHAgEWM2h0dHA6Ly93d3cu
# bWljcm9zb2Z0LmNvbS9wa2lvcHMvRG9jcy9SZXBvc2l0b3J5Lmh0bTAZBgkrBgEE
# AYI3FAIEDB4KAFMAdQBiAEMAQTAPBgNVHRMBAf8EBTADAQH/MB8GA1UdIwQYMBaA
# FMh+0mqFKhvKGZgEByfPUBBPaKiiMIGEBgNVHR8EfTB7MHmgd6B1hnNodHRwOi8v
# d3d3Lm1pY3Jvc29mdC5jb20vcGtpb3BzL2NybC9NaWNyb3NvZnQlMjBJZGVudGl0
# eSUyMFZlcmlmaWNhdGlvbiUyMFJvb3QlMjBDZXJ0aWZpY2F0ZSUyMEF1dGhvcml0
# eSUyMDIwMjAuY3JsMIHDBggrBgEFBQcBAQSBtjCBszCBgQYIKwYBBQUHMAKGdWh0
# dHA6Ly93d3cubWljcm9zb2Z0LmNvbS9wa2lvcHMvY2VydHMvTWljcm9zb2Z0JTIw
# SWRlbnRpdHklMjBWZXJpZmljYXRpb24lMjBSb290JTIwQ2VydGlmaWNhdGUlMjBB
# dXRob3JpdHklMjAyMDIwLmNydDAtBggrBgEFBQcwAYYhaHR0cDovL29uZW9jc3Au
# bWljcm9zb2Z0LmNvbS9vY3NwMA0GCSqGSIb3DQEBDAUAA4ICAQB/JSqe/tSr6t1m
# CttXI0y6XmyQ41uGWzl9xw+WYhvOL47BV09Dgfnm/tU4ieeZ7NAR5bguorTCNr58
# HOcA1tcsHQqt0wJsdClsu8bpQD9e/al+lUgTUJEV80Xhco7xdgRrehbyhUf4pkeA
# hBEjABvIUpD2LKPho5Z4DPCT5/0TlK02nlPwUbv9URREhVYCtsDM+31OFU3fDV8B
# mQXv5hT2RurVsJHZgP4y26dJDVF+3pcbtvh7R6NEDuYHYihfmE2HdQRq5jRvLE1E
# b59PYwISFCX2DaLZ+zpU4bX0I16ntKq4poGOFaaKtjIA1vRElItaOKcwtc04CBrX
# SfyL2Op6mvNIxTk4OaswIkTXbFL81ZKGD+24uMCwo/pLNhn7VHLfnxlMVzHQVL+b
# Ha9KhTyzwdG/L6uderJQn0cGpLQMStUuNDArxW2wF16QGZ1NtBWgKA8Kqv48M8Hf
# FqNifN6+zt6J0GwzvU8g0rYGgTZR8zDEIJfeZxwWDHpSxB5FJ1VVU1LIAtB7o9PX
# bjXzGifaIMYTzU4YKt4vMNwwBmetQDHhdAtTPplOXrnI9SI6HeTtjDD3iUN/7ygb
# ahmYOHk7VB7fwT4ze+ErCbMh6gHV1UuXPiLciloNxH6K4aMfZN1oLVk6YFeIJEok
# uPgNPa6EnTiOL60cPqfny+Fq8UiuZzGCGuwwghroAgEBMHEwWjELMAkGA1UEBhMC
# VVMxHjAcBgNVBAoTFU1pY3Jvc29mdCBDb3Jwb3JhdGlvbjErMCkGA1UEAxMiTWlj
# cm9zb2Z0IElEIFZlcmlmaWVkIENTIEFPQyBDQSAwNAITMwAEPI8bl0VqOg8iPwAA
# AAQ8jzANBglghkgBZQMEAgEFAKCBuDAZBgkqhkiG9w0BCQMxDAYKKwYBBAGCNwIB
# BDAcBgorBgEEAYI3AgELMQ4wDAYKKwYBBAGCNwIBFTAvBgkqhkiG9w0BCQQxIgQg
# Kld7dFLdvZygPQIm94QeWCWq09RhnYWpKs5R8/oLpjgwTAYKKwYBBAGCNwIBDDE+
# MDygNoA0AFAAeQB0AGgAbwBuACAAMwAuADEANQAuADAAcgBjADEAIAAoADMANwBl
# ADkAOABkAGEAKaECgAAwDQYJKoZIhvcNAQEBBQAEggGAMNV/HZOQ57XwufZa/pMg
# 8iuQ2DoHIU+lYelZpBXZRSs5omKKRsBQ4FChYOKC0+sVGx/U7g+iMh4aELBfVK9Q
# FqVX1MC10McarDUGnPDGD4kWLGiQVcrOWX23CssN588/JCwmhArJ3EX7Li+65DAi
# qq8k5TzyZFCouPG6ychYY3Wc782uCtD2J+tXIpzhhxfmHFklk0cQFi4SFS8Pa0EV
# y2DzHfMCNeT9+kgJPLwyWwClrtnoBNUEq97DkWJtKNcnircjMzWoC6krB4zKnyTJ
# z3AmqGaR4JJg7p2q6IYTY+CPGAbb5rgReoEHWgCeMGbzOFVQjfVrl/aNOjx2yWfM
# lR++AhCt+FeHfEtZEj/qhS3G0nVJC7+3PdL+T/9z3L7q6655WxCZl86dZWZjuQJ8
# 2DJ4hSgGxJWYAGhzV6y13w+oQuXVuo4FgTWSDwR3JAPlqorxBOkEDsHVA8wTGArC
# ziGXAgHP+Gi2MjCp0tME8zIts6oFKnLV3+sfBXtc+ZIXoYIYETCCGA0GCisGAQQB
# gjcDAwExghf9MIIX+QYJKoZIhvcNAQcCoIIX6jCCF+YCAQMxDzANBglghkgBZQME
# AgEFADCCAWIGCyqGSIb3DQEJEAEEoIIBUQSCAU0wggFJAgEBBgorBgEEAYRZCgMB
# MDEwDQYJYIZIAWUDBAIBBQAEIBbU7NBEcmQOvaVa1d/8fuIVCq0LNdnFSCTpSl4w
# qmyPAgZqaKVSrisYEzIwMjYwODA0MTIyOTMwLjM2N1owBIACAfSggeGkgd4wgdsx
# CzAJBgNVBAYTAlVTMRMwEQYDVQQIEwpXYXNoaW5ndG9uMRAwDgYDVQQHEwdSZWRt
# b25kMR4wHAYDVQQKExVNaWNyb3NvZnQgQ29ycG9yYXRpb24xJTAjBgNVBAsTHE1p
# Y3Jvc29mdCBBbWVyaWNhIE9wZXJhdGlvbnMxJzAlBgNVBAsTHm5TaGllbGQgVFNT
# IEVTTjo3QTAwLTA1RTAtRDk0NzE1MDMGA1UEAxMsTWljcm9zb2Z0IFB1YmxpYyBS
# U0EgVGltZSBTdGFtcGluZyBBdXRob3JpdHmggg8hMIIHgjCCBWqgAwIBAgITMwAA
# AAXlzw//Zi7JhwAAAAAABTANBgkqhkiG9w0BAQwFADB3MQswCQYDVQQGEwJVUzEe
# MBwGA1UEChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMUgwRgYDVQQDEz9NaWNyb3Nv
# ZnQgSWRlbnRpdHkgVmVyaWZpY2F0aW9uIFJvb3QgQ2VydGlmaWNhdGUgQXV0aG9y
# aXR5IDIwMjAwHhcNMjAxMTE5MjAzMjMxWhcNMzUxMTE5MjA0MjMxWjBhMQswCQYD
# VQQGEwJVUzEeMBwGA1UEChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMTIwMAYDVQQD
# EylNaWNyb3NvZnQgUHVibGljIFJTQSBUaW1lc3RhbXBpbmcgQ0EgMjAyMDCCAiIw
# DQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAJ5851Jj/eDFnwV9Y7UGIqMcHtfn
# lzPREwW9ZUZHd5HBXXBvf7KrQ5cMSqFSHGqg2/qJhYqOQxwuEQXG8kB41wsDJP5d
# 0zmLYKAY8Zxv3lYkuLDsfMuIEqvGYOPURAH+Ybl4SJEESnt0MbPEoKdNihwM5xGv
# 0rGofJ1qOYSTNcc55EbBT7uq3wx3mXhtVmtcCEr5ZKTkKKE1CxZvNPWdGWJUPC6e
# 4uRfWHIhZcgCsJ+sozf5EeH5KrlFnxpjKKTavwfFP6XaGZGWUG8TZaiTogRoAlqc
# evbiqioUz1Yt4FRK53P6ovnUfANjIgM9JDdJ4e0qiDRm5sOTiEQtBLGd9Vhd1Mad
# xoGcHrRCsS5rO9yhv2fjJHrmlQ0EIXmp4DhDBieKUGR+eZ4CNE3ctW4uvSDQVeSp
# 9h1SaPV8UWEfyTxgGjOsRpeexIveR1MPTVf7gt8hY64XNPO6iyUGsEgt8c2PxF87
# E+CO7A28TpjNq5eLiiunhKbq0XbjkNoU5JhtYUrlmAbpxRjb9tSreDdtACpm3rkp
# xp7AQndnI0Shu/fk1/rE3oWsDqMX3jjv40e8KN5YsJBnczyWB4JyeeFMW3JBfdeA
# KhzohFe8U5w9WuvcP1E8cIxLoKSDzCCBOu0hWdjzKNu8Y5SwB1lt5dQhABYyzR3d
# xEO/T1K/BVF3rV69AgMBAAGjggIbMIICFzAOBgNVHQ8BAf8EBAMCAYYwEAYJKwYB
# BAGCNxUBBAMCAQAwHQYDVR0OBBYEFGtpKDo1L0hjQM972K9J6T7ZPdshMFQGA1Ud
# IARNMEswSQYEVR0gADBBMD8GCCsGAQUFBwIBFjNodHRwOi8vd3d3Lm1pY3Jvc29m
# dC5jb20vcGtpb3BzL0RvY3MvUmVwb3NpdG9yeS5odG0wEwYDVR0lBAwwCgYIKwYB
# BQUHAwgwGQYJKwYBBAGCNxQCBAweCgBTAHUAYgBDAEEwDwYDVR0TAQH/BAUwAwEB
# /zAfBgNVHSMEGDAWgBTIftJqhSobyhmYBAcnz1AQT2ioojCBhAYDVR0fBH0wezB5
# oHegdYZzaHR0cDovL3d3dy5taWNyb3NvZnQuY29tL3BraW9wcy9jcmwvTWljcm9z
# b2Z0JTIwSWRlbnRpdHklMjBWZXJpZmljYXRpb24lMjBSb290JTIwQ2VydGlmaWNh
# dGUlMjBBdXRob3JpdHklMjAyMDIwLmNybDCBlAYIKwYBBQUHAQEEgYcwgYQwgYEG
# CCsGAQUFBzAChnVodHRwOi8vd3d3Lm1pY3Jvc29mdC5jb20vcGtpb3BzL2NlcnRz
# L01pY3Jvc29mdCUyMElkZW50aXR5JTIwVmVyaWZpY2F0aW9uJTIwUm9vdCUyMENl
# cnRpZmljYXRlJTIwQXV0aG9yaXR5JTIwMjAyMC5jcnQwDQYJKoZIhvcNAQEMBQAD
# ggIBAF+Idsd+bbVaFXXnTHho+k7h2ESZJRWluLE0Oa/pO+4ge/XEizXvhs0Y7+KV
# Yyb4nHlugBesnFqBGEdC2IWmtKMyS1OWIviwpnK3aL5JedwzbeBF7POyg6IGG/Xh
# hJ3UqWeWTO+Czb1c2NP5zyEh89F72u9UIw+IfvM9lzDmc2O2END7MPnrcjWdQnrL
# n1Ntday7JSyrDvBdmgbNnCKNZPmhzoa8PccOiQljjTW6GePe5sGFuRHzdFt8y+bN
# 2neF7Zu8hTO1I64XNGqst8S+w+RUdie8fXC1jKu3m9KGIqF4aldrYBamyh3g4nJP
# j/LR2CBaLyD+2BuGZCVmoNR/dSpRCxlot0i79dKOChmoONqbMI8m04uLaEHAv4qw
# KHQ1vBzbV/nG89LDKbRSSvijmwJwxRxLLpMQ/u4xXxFfR4f/gksSkbJp7oqLwliD
# m/h+w0aJ/U5ccnYhYb7vPKNMN+SZDWycU5ODIRfyoGl59BsXR/HpRGtiJquOYGmv
# A/pk5vC1lcnbeMrcWD/26ozePQ/TWfNXKBOmkFpvPE8CH+EeGGWzqTCjdAsno2jz
# TeNSxlx3glDGJgcdz5D/AAxw9Sdgq/+rY7jjgs7X6fqPTXPmaCAJKVHAP19oEjJI
# BwD1LyHbaEgBxFCogYSOiUIr0Xqcr1nJfiWG2GwYe6ZoAF1bMIIHlzCCBX+gAwIB
# AgITMwAAAFhlzes/odf80gAAAAAAWDANBgkqhkiG9w0BAQwFADBhMQswCQYDVQQG
# EwJVUzEeMBwGA1UEChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMTIwMAYDVQQDEylN
# aWNyb3NvZnQgUHVibGljIFJTQSBUaW1lc3RhbXBpbmcgQ0EgMjAyMDAeFw0yNTEw
# MjMyMDQ2NTVaFw0yNjEwMjIyMDQ2NTVaMIHbMQswCQYDVQQGEwJVUzETMBEGA1UE
# CBMKV2FzaGluZ3RvbjEQMA4GA1UEBxMHUmVkbW9uZDEeMBwGA1UEChMVTWljcm9z
# b2Z0IENvcnBvcmF0aW9uMSUwIwYDVQQLExxNaWNyb3NvZnQgQW1lcmljYSBPcGVy
# YXRpb25zMScwJQYDVQQLEx5uU2hpZWxkIFRTUyBFU046N0EwMC0wNUUwLUQ5NDcx
# NTAzBgNVBAMTLE1pY3Jvc29mdCBQdWJsaWMgUlNBIFRpbWUgU3RhbXBpbmcgQXV0
# aG9yaXR5MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAnXg0pHaQ7PVA
# lln+HZZrJFcLoKbekhW1yL+QNBUgFFUsjZIKaqqN4oIJsJM3ps0rJNSO7ndCNRuZ
# DX2Wgur3Ak77eXrloBXqZmO6ZVXeDNRCLldW4A0/NfjzJ7XXkdEhjr81ghXEpR7z
# C+wbaNN+sPSxzLAZBeibDFP7Xws5wX0ZtIsN1a2+Xq5bvWp3kRMytwskTjunRgeL
# ZL/tBp237JVdRPFAQ9jYRKpCqUBo/v1xjBLRCV3PalKjnGfb3MN4U7jVyqifFHSh
# cnW5CERRoBmUa6sygDzFSr8e3g93TPNLFUivUE0GmLfbX5ceD1Gt1FcZ6x/JLVAT
# zk5+BWHbMxwJIVkVPTqSSMjQ6KTKdcnq3pH0c4AFJp/glvcpq0U9fzZIjJGGvdpi
# shlRl77RQtUhSjxHvCn3LC/xqQQwOHSQDsGh6NX2D0RfsSyEtTAByAae+2w1HByT
# DTcmlTNLEuQLeCj1gNBdIWj0WOYyDtjjQ/8iTWY6ey1vb9qHljIj5HgIndT5P9MY
# k2Vg2e7hKUZNBNbA/hsgBsuoZ+IX89WvjEN9abF91S4OJVuinmKsLO/MLbnl7iku
# D0dN6oA0YewyDQncs12sM9HOtu72QA/TZlefvW8r9xtMXAYoQlcGjsk8W4Uc7cfq
# VqbIPjdoc8ZxBzLcXcVyP4p5cyLwvkMCAwEAAaOCAcswggHHMB0GA1UdDgQWBBRy
# jU3Fer4VxXJ+hjPcRJnxnRIJsDAfBgNVHSMEGDAWgBRraSg6NS9IY0DPe9ivSek+
# 2T3bITBsBgNVHR8EZTBjMGGgX6BdhltodHRwOi8vd3d3Lm1pY3Jvc29mdC5jb20v
# cGtpb3BzL2NybC9NaWNyb3NvZnQlMjBQdWJsaWMlMjBSU0ElMjBUaW1lc3RhbXBp
# bmclMjBDQSUyMDIwMjAuY3JsMHkGCCsGAQUFBwEBBG0wazBpBggrBgEFBQcwAoZd
# aHR0cDovL3d3dy5taWNyb3NvZnQuY29tL3BraW9wcy9jZXJ0cy9NaWNyb3NvZnQl
# MjBQdWJsaWMlMjBSU0ElMjBUaW1lc3RhbXBpbmclMjBDQSUyMDIwMjAuY3J0MAwG
# A1UdEwEB/wQCMAAwFgYDVR0lAQH/BAwwCgYIKwYBBQUHAwgwDgYDVR0PAQH/BAQD
# AgeAMGYGA1UdIARfMF0wUQYMKwYBBAGCN0yDfQEBMEEwPwYIKwYBBQUHAgEWM2h0
# dHA6Ly93d3cubWljcm9zb2Z0LmNvbS9wa2lvcHMvRG9jcy9SZXBvc2l0b3J5Lmh0
# bTAIBgZngQwBBAIwDQYJKoZIhvcNAQEMBQADggIBAHvrxIiVF1iHcXvxrJTCD8eO
# tbPUbK9x+Lz70iYehh+G0UoOMcMf04QD6tQPTeZ5HhGETkcn0raDJ5NpfbRBuKEH
# 31rxbZK97o12KRDNJ3Nu4ePaUIpH/TcWz8PLVOCECywSxbEgEG20kyydGc46c591
# tXzpfkJDckjoYrypaerdeQLRQH9LaoTYZfdAzMo+Dy0O1DzFJkF5YnsmAM8lt9r1
# NtXdFjdbFMCbV5dau64mV22s186A8Umi+l239+Ue0cbJQIykWhIlhhWhxQgoksqH
# z7kp2GFZAAeySTmIOQOWyXOA8JA8TISJyn3JDOgStv583P3V0QSALT6JXDCW26FV
# 208VGJMzkv0S22iOTZJ/oamTpk8RzD8oWT8pfbe1q/k/bxPiXYRbzps96a5YOko7
# n0Vdo61DOJhL/mhk01Y348gq6vhG/VTcdGHh1rCkwOM05B35AZZq9AtPpfRzJinr
# HzzGRx+r6fD3ccYMPMMX/Nwd2irzrph172fQcSf1fMwvwIhmfH4GWJJ+mf1HA6uX
# oAOVByckguXvlj8gPi7T2ES6RU8+QssfqTNTJKjsBheWKWv2W4ESVen2L7lCz7i7
# 9FhA+0kp0yXJnYwdzWS0ovTINULINmzVyMcSUm5WuVf8YZ33cAud2Opr6N1+RuLZ
# DavDvjiehlI5dH+GEy56MYIHQzCCBz8CAQEweDBhMQswCQYDVQQGEwJVUzEeMBwG
# A1UEChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMTIwMAYDVQQDEylNaWNyb3NvZnQg
# UHVibGljIFJTQSBUaW1lc3RhbXBpbmcgQ0EgMjAyMAITMwAAAFhlzes/odf80gAA
# AAAAWDANBglghkgBZQMEAgEFAKCCBJwwEQYLKoZIhvcNAQkQAg8xAgUAMBoGCSqG
# SIb3DQEJAzENBgsqhkiG9w0BCRABBDAcBgkqhkiG9w0BCQUxDxcNMjYwODA0MTIy
# OTMwWjAvBgkqhkiG9w0BCQQxIgQgshmlIDZmXyXFgDr32r5A6WJxYXOH6vGWza3v
# I3CgTnYwgbkGCyqGSIb3DQEJEAIvMYGpMIGmMIGjMIGgBCDFIlS7sgfQ+wAo1cWb
# Wz+WN69VBds58hbran919aLocTB8MGWkYzBhMQswCQYDVQQGEwJVUzEeMBwGA1UE
# ChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMTIwMAYDVQQDEylNaWNyb3NvZnQgUHVi
# bGljIFJTQSBUaW1lc3RhbXBpbmcgQ0EgMjAyMAITMwAAAFhlzes/odf80gAAAAAA
# WDCCA14GCyqGSIb3DQEJEAISMYIDTTCCA0mhggNFMIIDQTCCAikCAQEwggEJoYHh
# pIHeMIHbMQswCQYDVQQGEwJVUzETMBEGA1UECBMKV2FzaGluZ3RvbjEQMA4GA1UE
# BxMHUmVkbW9uZDEeMBwGA1UEChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMSUwIwYD
# VQQLExxNaWNyb3NvZnQgQW1lcmljYSBPcGVyYXRpb25zMScwJQYDVQQLEx5uU2hp
# ZWxkIFRTUyBFU046N0EwMC0wNUUwLUQ5NDcxNTAzBgNVBAMTLE1pY3Jvc29mdCBQ
# dWJsaWMgUlNBIFRpbWUgU3RhbXBpbmcgQXV0aG9yaXR5oiMKAQEwBwYFKw4DAhoD
# FQCdZHkb26ercF2O62vCdZUfUSvEXKBnMGWkYzBhMQswCQYDVQQGEwJVUzEeMBwG
# A1UEChMVTWljcm9zb2Z0IENvcnBvcmF0aW9uMTIwMAYDVQQDEylNaWNyb3NvZnQg
# UHVibGljIFJTQSBUaW1lc3RhbXBpbmcgQ0EgMjAyMDANBgkqhkiG9w0BAQsFAAIF
# AO4btYswIhgPMjAyNjA4MDQwMDQ5MTVaGA8yMDI2MDgwNTAwNDkxNVowdDA6Bgor
# BgEEAYRZCgQBMSwwKjAKAgUA7hu1iwIBADAHAgEAAgIZpzAHAgEAAgISfjAKAgUA
# 7h0HCwIBADA2BgorBgEEAYRZCgQCMSgwJjAMBgorBgEEAYRZCgMCoAowCAIBAAID
# B6EgoQowCAIBAAIDAYagMA0GCSqGSIb3DQEBCwUAA4IBAQAwF/B+RL4am1iKPQNB
# gxr+1s60tqK2QouteXjVjMyX6jsJbvMnvbDT2E29pJod9MKqfVlF3JeDvERPYHYF
# 51GMifg3uLuOFzr9PfsCgQhRDmezDT1WpZQHOc00Z/JI4+3n3JGMPxyF34GP9ZMp
# D+9Dk2cghADVgVikYLLO7RYarEik0kva6Sf6vvFUt5MNdqLxxcl1JlHDleBlbb3B
# SBLA+jz1IuBeN8N4E1vhP/iotT3mT0ENpAi08sre8l2K79ALYOclkmTou2GmVz2k
# tWEub2iW1sZe1xMIKGeUiytP2xLFTtvTy9ehNvz5b7w6+ChoXRHH6vOCzu/xnquZ
# XO8DMA0GCSqGSIb3DQEBAQUABIICAG/ByLj0a8y1RnNtiGh4u/DzDqLFfF9SkEuJ
# 5Uoc3B3qrWSGB2IkWb5/bX6NJqMz7k8xV2DtZ1H3oxIislGYthY7Je8qNSl+aq5p
# nP8gqnZNAw1Y//BKwMoEr5sQr1yCf2GLzhcCW3J+bnRnwxqiScJnEqrb/e35POmJ
# WcnmheVZUsmYq2YlNSYbJt1AXvXAATIIefyyCLzszBevuemougtHNHddRJHcp57E
# WueEtMcYKuzBAwIQChrXKQVP65JYFwcOm9O5B47i+kEbOr3PK+j5LCfP4aNadoUb
# 2HZ95+ixX7FtRzTMipcoVXbfifLS7XdVoRjcELpKavpPTQPJjxmOnVQOE+GtAjFF
# 4TJB53V6BSCdXCigI0iaRem00GYaTCPNtanVOKOj20SVw3q/gs+ySxvAFxqdMmyr
# Ibfpe9RDhIhds/+pmO2SFQNnTymPrBURpKm73Wq3K3eTFmrD7v/tAgfACm/ZvJMC
# kNGH86hU3aujFrPDS2fQVviHztEffrFzmKEVwUv6yj+Uk/UaopP74g8gN3QOwi26
# rcETmJvXAGBFzXqAMS3n6bq3pvckRtQ+u8bJFP17zb0zUlYOo1F8oILM5L1Y4qSq
# h8FvZ/Cz844czRCTZY7skVLHShr08kSSKhmCpJPOFktBUkS85l/dLYaDiNd1GZJy
# /T4mgEcg
# SIG # End signature block
