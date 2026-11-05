import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { existsSync } from "node:fs";
import path from "node:path";

const run = promisify(execFile);

/** Payload root: the relay repo this plugin ships (parent of dist/). */
function payloadRoot(): string {
  return path.resolve(import.meta.dirname ?? ".", "..");
}

function installerPath(): string {
  return path.join(payloadRoot(), "install_relay.py");
}

async function pyRun(args: string[], timeoutMs = 180000): Promise<string> {
  const { stdout, stderr } = await run("python3", args, { timeout: timeoutMs });
  return [stdout, stderr].filter(Boolean).join("\n").trim();
}

async function firstTailscaleCli(): Promise<string | null> {
  for (const ts of [
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
  ]) {
    try {
      await run(ts, ["version"], { timeout: 15000 });
      return ts;
    } catch {
      /* try next candidate */
    }
  }
  return null;
}

type RelayStatus = {
  relayRunning: boolean;
  listeners: string;
  serveRoute: string | null;
  installerPresent: boolean;
};

async function gatherStatus(): Promise<RelayStatus> {
  let listeners = "nothing listening on 4001/4002 (lsof empty)";
  let relayRunning = false;
  try {
    const { stdout } = await run("lsof", [
      "-nP",
      "-iTCP:4001",
      "-iTCP:4002",
      "-sTCP:LISTEN",
    ]);
    listeners = stdout.trim() || listeners;
    relayRunning = listeners.includes("4001") && listeners.includes("4002");
  } catch {
    /* lsof unavailable or nothing found */
  }

  let serveRoute: string | null = null;
  const ts = await firstTailscaleCli();
  if (ts) {
    try {
      const { stdout } = await run(ts, ["serve", "status"]);
      serveRoute =
        stdout
          .split("\n")
          .find((l) => l.includes(":8444"))
          ?.trim() ?? null;
    } catch {
      /* serve status unavailable */
    }
  }

  return { relayRunning, listeners, serveRoute, installerPresent: existsSync(installerPath()) };
}

export default defineToolPlugin({
  id: "esp-serial-relay",
  name: "ESP Serial Relay",
  description:
    "Flash and monitor ESP boards from any tailnet machine — one browser tab holds the serial line over RFC2217, with a live phase panel, write progress, and capture/replay. No extra software on the desk.",
  // v25: settings live in the OpenClaw web GUI (Control UI renders this schema
  // under plugins.entries.esp-serial-relay.config); the install tool forwards
  // them to the installer → LaunchAgent EnvironmentVariables → daemon boot.
  configSchema: Type.Object({
    rfcPort: Type.Optional(
      Type.Integer({
        description: "RFC2217 port esptool connects to (rfc2217://…:PORT). Default 4001.",
        minimum: 1,
        maximum: 65535,
        default: 4001,
      }),
    ),
    wsPort: Type.Optional(
      Type.Integer({
        description: "Web/WS port for the relay GUI + dashboard. Default 4002.",
        minimum: 1,
        maximum: 65535,
        default: 4002,
      }),
    ),
    rfcBind: Type.Optional(
      Type.String({
        description:
          "RFC2217 bind address. Default 127.0.0.1 (loopback) — widening it exposes the flash port to the network.",
        default: "127.0.0.1",
      }),
    ),
    browserWait: Type.Optional(
      Type.Integer({
        description:
          "Seconds the daemon waits for the serial tab to (re)connect before dropping flash payload. Default 30.",
        minimum: 1,
        maximum: 600,
        default: 30,
      }),
    ),
  }),
  tools: (tool) => [
    tool({
      name: "esp_relay_status",
      description:
        "Report the ESP serial relay state on this machine: relay listeners (4001 RFC2217 / 4002 dashboard), tailscale serve route (8444), installer presence.",
      parameters: Type.Object({}),
      execute: async (_params, cfgRaw) => {
        const cfg = (cfgRaw ?? {}) as Record<string, unknown>;
        const s = await gatherStatus();
        return {
          content: [
            {
              type: "text",
              text: [
                `relayRunning: ${s.relayRunning}`,
                `listeners: ${s.listeners}`,
                `serveRoute: ${s.serveRoute ?? "none"}`,
                `installerPresent: ${s.installerPresent}`,
                `config: rfcPort=${cfg.rfcPort ?? "default(4001)"} wsPort=${cfg.wsPort ?? "default(4002)"} rfcBind=${cfg.rfcBind ?? "default(127.0.0.1)"} browserWait=${cfg.browserWait ?? "default(30)"}`,
              ].join("\n"),
            },
          ],
        };
      },
    }),
    tool({
      name: "esp_relay_install",
      description:
        "Install the ESP serial relay on this machine (field-verified install procedure): python deps, LaunchAgent app.clawkie.serial-relay with re-homed paths, 4001/4002 listener gate, tailscale serve route 8444.",
      parameters: Type.Object({
        skipServe: Type.Optional(
          Type.Boolean({ description: "Skip the tailscale serve route (default false)." }),
        ),
        dryRun: Type.Optional(
          Type.Boolean({ description: "Walk through without changing anything." }),
        ),
      }),
      execute: async ({ skipServe, dryRun }, cfgRaw) => {
        const cfg = (cfgRaw ?? {}) as Record<string, unknown>;
        const args = [installerPath()];
        // v25: configured knobs flow to the installer → LaunchAgent → daemon
        const knobs: Array<[string, string | number]> = [
          ["--rfc-port", cfg.rfcPort as string | number],
          ["--ws-port", cfg.wsPort as string | number],
          ["--rfc-bind", cfg.rfcBind as string],
          ["--browser-wait", cfg.browserWait as string | number],
        ];
        for (const [flag, val] of knobs) {
          if (val !== undefined && val !== null) args.push(flag, String(val));
        }
        if (skipServe) args.push("--skip-serve");
        if (dryRun) args.push("--dry-run");
        const out = await pyRun(args);
        return { content: [{ type: "text", text: out || "installer produced no output" }] };
      },
    }),
  ],
});