export const ankiConnectUrl = (process.env.ANKI_CONNECT_URL || "http://127.0.0.1:8765").replace(/\/+$/, "");
export const stdinTimeoutMs = clampInt(process.env.MEDNOTES_HOOK_STDIN_TIMEOUT_MS, 250, 100, 2000);
export const stdinHardTimeoutMs = clampInt(process.env.MEDNOTES_HOOK_STDIN_HARD_TIMEOUT_MS, 2500, 250, 15000);
export const ankiStartTimeoutMs = clampInt(process.env.MEDNOTES_ANKI_START_TIMEOUT_MS, 8000, 1000, 15000);
export const ankiAutoStart = /^(1|true|yes)$/i.test(process.env.MEDNOTES_ANKI_AUTO_START || "");
const EAGER_PARSE_MAX_CHARS = 64 * 1024;

export function clampInt(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
}

export function quiet() {
  return { suppressOutput: true };
}

export function allow(extra = {}) {
  return { decision: "allow", ...extra };
}

export function deny(reason, extra = {}) {
  return {
    decision: "deny",
    reason: String(reason || "Operação bloqueada pela política de segurança do vault."),
    ...extra,
  };
}

export function writeJson(output) {
  process.stdout.write(JSON.stringify(output || quiet()));
}

export function tryParseJson(text) {
  try {
    return { ok: true, value: JSON.parse(text || "{}") };
  } catch {
    return { ok: false, value: {} };
  }
}

export async function readPayload(timeoutMs = stdinTimeoutMs) {
  return (await readPayloadResult(timeoutMs)).payload;
}

export function readPayloadResult(timeoutMs = stdinTimeoutMs, hardTimeoutMs = stdinHardTimeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    let text = "";
    let idleTimer;
    let hardTimer;

    const finish = (payload, error = null) => {
      if (settled) return;
      settled = true;
      clearTimeout(idleTimer);
      clearTimeout(hardTimer);
      try {
        process.stdin.pause();
        process.stdin.unref?.();
        process.stdin.removeAllListeners("data");
        process.stdin.removeAllListeners("end");
        process.stdin.removeAllListeners("error");
      } catch {
        // Best effort: the hook must still return JSON even with unusual stdin.
      }
      resolve({
        payload: payload && typeof payload === "object" ? payload : {},
        error,
      });
    };

    const parseOrError = (raw, options = {}) => {
      const parsed = tryParseJson(raw.trim());
      if (parsed.ok) {
        finish(parsed.value);
      } else {
        const reason = options.reason || "unknown";
        finish(
          {},
          {
            type: "invalid_stdin_json",
            message: "Hook stdin was not valid JSON.",
            details: {
              reason,
              stdin_bytes: Buffer.byteLength(raw || "", "utf8"),
            },
          },
        );
      }
    };

    const parseWhenComplete = () => {
      if (!shouldAttemptParse(text)) return false;
      const parsed = tryParseJson(text.trim());
      if (!parsed.ok) return false;
      finish(parsed.value);
      return true;
    };

    const handleIdleTimeout = (reason = "timeout") => {
      if (settled) return;
      if (!text.trim()) {
        finish({});
      } else if (isSmallPayload(text)) {
        parseOrError(text, { reason });
      } else {
        if (parseWhenComplete()) return;
        // Gemini can split or truncate large hook stdin. Partial large JSON is
        // a transport limitation, not a workflow or model error to report.
      }
    };

    const armIdleTimeout = (reason = "timeout") => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => handleIdleTimeout(reason), timeoutMs);
    };

    armIdleTimeout("timeout");
    hardTimer = setTimeout(() => {
      if (settled) return;
      if (!text.trim()) {
        finish({});
      } else if (isSmallPayload(text)) {
        parseOrError(text, { reason: "hard_timeout" });
      } else if (parseWhenComplete()) {
        return;
      } else {
        finish({});
      }
    }, hardTimeoutMs);

    if (process.stdin.isTTY) {
      finish({});
      return;
    }

    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      text += chunk;
      armIdleTimeout("inactivity_timeout");
      parseWhenComplete();
    });
    process.stdin.on("end", () => {
      if (text.trim()) {
        parseOrError(text, { reason: "end" });
      } else {
        finish({});
      }
    });
    process.stdin.on("error", (error) =>
      finish(
        {},
        {
          type: "stdin_read_error",
          message: error instanceof Error ? error.message : String(error || "Hook stdin read failed."),
          details: { stdin_bytes: Buffer.byteLength(text || "", "utf8") },
        },
      ),
    );
    process.stdin.resume();
  });
}

function shouldAttemptParse(raw) {
  const trimmed = String(raw || "").trim();
  if (!trimmed) return true;
  if (isSmallPayload(trimmed)) return true;
  return trimmed.endsWith("}") || trimmed.endsWith("]");
}

function isSmallPayload(raw) {
  return String(raw || "").trim().length <= EAGER_PARSE_MAX_CHARS;
}
