from __future__ import annotations

import json


def render_bookmarklet(host: str, port: int, token: str) -> str:
    if not token:
        raise ValueError("token is required.")
    endpoint = f"http://{host}:{port}/capture"
    headers = {"Content-Type": "application/json", "X-Obsidian-Web-Clipper-Token": token}
    script = f"""
(() => {{
  const endpoint = {json.dumps(endpoint)};
  const headers = {json.dumps(headers)};
  const existing = document.getElementById("obsidian-web-clipper-panel");
  if (existing) existing.remove();

  const panel = document.createElement("div");
  panel.id = "obsidian-web-clipper-panel";
  panel.style.cssText = "position:fixed;z-index:2147483647;right:16px;top:16px;width:340px;background:#fff;color:#111;border:1px solid #bbb;box-shadow:0 8px 30px rgba(0,0,0,.24);font:14px system-ui,sans-serif;padding:12px;";
  panel.innerHTML = `
    <div style="font-weight:700;margin-bottom:8px;">Web clip to Obsidian</div>
    <div style="font-size:12px;margin-bottom:8px;word-break:break-all;">${{endpoint}}</div>
    <label style="display:block;margin-bottom:6px;">Why
      <textarea id="owc-why" style="box-sizing:border-box;width:100%;min-height:72px;"></textarea>
    </label>
    <label style="display:block;margin-bottom:6px;">Passages
      <textarea id="owc-passages" style="box-sizing:border-box;width:100%;min-height:120px;"></textarea>
    </label>
    <div style="display:flex;gap:8px;justify-content:flex-end;">
      <button type="button" id="owc-add">Add passage</button>
      <button type="button" id="owc-save">Save</button>
      <button type="button" id="owc-close">Close</button>
    </div>
    <div id="owc-status" style="font-size:12px;margin-top:8px;"></div>
  `;

  document.body.appendChild(panel);
  const passages = panel.querySelector("#owc-passages");
  const status = panel.querySelector("#owc-status");
  const selection = String(window.getSelection ? window.getSelection() : "").trim();
  if (selection) passages.value = selection;

  panel.querySelector("#owc-add").addEventListener("click", () => {{
    const selected = String(window.getSelection ? window.getSelection() : "").trim();
    if (!selected) return;
    passages.value = passages.value ? `${{passages.value}}\\n\\n${{selected}}` : selected;
  }});

  panel.querySelector("#owc-close").addEventListener("click", () => panel.remove());
  panel.querySelector("#owc-save").addEventListener("click", async () => {{
    const payload = {{
      source_url: location.href,
      source_title: document.title || location.href,
      captured_at: new Date().toISOString(),
      why: panel.querySelector("#owc-why").value,
      passages: passages.value.split(/\\n\\s*\\n/).map((text) => text.trim()).filter(Boolean),
    }};
    status.textContent = "Saving...";
    const response = await fetch(endpoint, {{
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    }});
    status.textContent = response.ok ? "Saved." : `Failed: ${{await response.text()}}`;
  }});
}})();
"""
    return f"javascript:{script}"
