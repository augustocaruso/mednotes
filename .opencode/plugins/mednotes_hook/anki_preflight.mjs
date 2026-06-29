import { spawn } from "node:child_process";
import http from "node:http";

import { ankiAutoStart, ankiConnectUrl, ankiStartTimeoutMs, quiet } from "./runtime.mjs";

export function isAnkiTool(payload) {
  const toolName = String(payload.tool_name || payload.toolName || payload.name || "");
  if (/^mcp_anki(?:-mcp)?_/.test(toolName)) return true;
  const fields = [
    payload.tool_name,
    payload.toolName,
    payload.name,
    payload.server_name,
    payload.serverName,
    payload.mcp_server_name,
    payload.mcpServerName,
    payload.tool?.name,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return /\banki\b/.test(fields);
}

export function ankiConnectReady(timeoutMs = 800) {
  return new Promise((resolve) => {
    const body = JSON.stringify({ action: "version", version: 6 });
    const request = http.request(
      ankiConnectUrl,
      {
        method: "POST",
        timeout: timeoutMs,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (response) => {
        let text = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          text += chunk;
        });
        response.on("end", () => {
          try {
            const parsed = JSON.parse(text || "{}");
            resolve(response.statusCode === 200 && parsed.error == null && parsed.result != null);
          } catch {
            resolve(false);
          }
        });
      },
    );
    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });
    request.on("error", () => resolve(false));
    request.write(body);
    request.end();
  });
}

function runProcess(command, args, timeoutMs) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });
    } catch (error) {
      resolve({ ok: false, message: error instanceof Error ? error.message : String(error) });
      return;
    }

    let stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      resolve({ ok: false, timedOut: true, message: stderr.trim() });
    }, timeoutMs);

    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ ok: false, message: error instanceof Error ? error.message : String(error) });
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      resolve({ ok: code === 0, message: stderr.trim() });
    });
  });
}

export async function launchAnki() {
  if (process.platform === "win32") {
    const script = String.raw`
$ErrorActionPreference = "SilentlyContinue"
$programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
$paths = @(
  "$env:LOCALAPPDATA\Programs\Anki\anki.exe",
  "$env:ProgramFiles\Anki\anki.exe",
  $(if ($programFilesX86) { Join-Path $programFilesX86 "Anki\anki.exe" })
)
$running = (Get-Process -Name "anki" -ErrorAction SilentlyContinue | Select-Object -First 1) -or (Get-Process | Where-Object { $_.MainWindowTitle -match "Anki" } | Select-Object -First 1)
if (-not $running) {
  $ankiPath = $paths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
  if ($ankiPath) {
    Start-Process -FilePath $ankiPath -WindowStyle Minimized
  } else {
    Write-Error "anki.exe not found in standard paths"
    exit 2
  }
}
$code = @"
using System;
using System.Runtime.InteropServices;
public class Win32Anki {
  [DllImport("user32.dll")]
  [return: MarshalAs(UnmanagedType.Bool)]
  public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@
try { Add-Type -TypeDefinition $code -ErrorAction SilentlyContinue } catch { }

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$ankiReady = $false
$windowMinimized = $false
$ankiWindow = $null
while ($stopwatch.Elapsed.TotalSeconds -lt 20) {
  if (-not $ankiReady) {
    try {
      Invoke-RestMethod -Uri "http://127.0.0.1:8765" -Method Get -TimeoutSec 1 -ErrorAction Stop | Out-Null
      $ankiReady = $true
    } catch { }
  }

  if (-not $windowMinimized) {
    $ankiWindow = Get-Process | Where-Object { $_.MainWindowTitle -match "Anki" -and $_.MainWindowHandle -ne [IntPtr]::Zero } | Select-Object -First 1
    if ($ankiWindow) {
      try {
        [Win32Anki]::ShowWindow($ankiWindow.MainWindowHandle, 6) | Out-Null
        $windowMinimized = $true
      } catch { }
    }
  }

  if ($ankiReady -and $windowMinimized) {
    Start-Sleep -Milliseconds 500
    if ($ankiWindow) {
      try { [Win32Anki]::ShowWindow($ankiWindow.MainWindowHandle, 6) | Out-Null } catch { }
    }
    break
  }
  Start-Sleep -Milliseconds 200
}
exit 0
`;
    const result = await runProcess(
      "powershell.exe",
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
      ankiStartTimeoutMs + 1000,
    );
    if (!result.ok && result.message) console.error(result.message);
    return result.ok;
  }

  if (process.platform === "darwin") {
    const result = await runProcess("open", ["-g", "-j", "-a", "Anki"], 3000);
    if (!result.ok && result.message) console.error(result.message);
    return result.ok;
  }

  try {
    const child = spawn("anki", [], { detached: true, stdio: "ignore" });
    child.unref();
    return true;
  } catch (error) {
    console.error(`Could not start Anki: ${error instanceof Error ? error.message : String(error)}`);
    return false;
  }
}

export async function waitForAnkiConnect(timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await ankiConnectReady()) return true;
    await new Promise((resolve) => setTimeout(resolve, 350));
  }
  return false;
}

export async function ensureAnkiBefore(payload) {
  if (!isAnkiTool(payload)) return quiet();
  if (await ankiConnectReady()) return quiet();
  if (!ankiAutoStart) return quiet();

  console.error("AnkiConnect is not ready. Trying a bounded Anki preflight before Anki MCP tool use.");
  const launched = await launchAnki();
  const ready = launched ? await waitForAnkiConnect(ankiStartTimeoutMs) : false;

  if (ready) {
    return {
      systemMessage: "Anki foi aberto; AnkiConnect esta pronto para usar o MCP.",
      suppressOutput: true,
    };
  }

  return {
    systemMessage:
      "AnkiConnect nao respondeu; a ferramenta Anki MCP pode falhar. Abra o Anki Desktop e confira o add-on AnkiConnect.",
    suppressOutput: true,
  };
}
