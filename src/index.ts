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
    "Install, inspect, and remove the ESP serial relay bridge (browser WebSerial → RFC2217 :4001 for esptool, dashboard :4002, tailscale serve :8444).",
  tools: (tool) => [
    tool({
      name: "esp_relay_status",
      description:
        "Report the ESP serial relay state on this machine: relay listeners (4001 RFC2217 / 4002 dashboard), tailscale serve route (8444), installer presence.",
      parameters: Type.Object({}),
      execute: async () => {
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
      execute: async ({ skipServe, dryRun }) => {
        const args = [installerPath()];
        if (skipServe) args.push("--skip-serve");
        if (dryRun) args.push("--dry-run");
        const out = await pyRun(args);
        return { content: [{ type: "text", text: out || "installer produced no output" }] };
      },
    }),
  ],
});