import { useState } from "react";
import { ExternalLink, Copy, Check, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, startOAuth, API } from "@/lib/api";

export default function SettingsDialog({ open, onOpenChange, settings, onSaved }) {
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState(false);

  const redirectUrl = settings?.redirect_uri || `${API}/auth/twitch/callback`;

  // --- Buffer state ---
  const [bufferKey, setBufferKey] = useState("");
  const [bufferSaving, setBufferSaving] = useState(false);
  const [bufferFetching, setBufferFetching] = useState(false);
  const [bufferChannels, setBufferChannels] = useState([]);

  const save = async () => {
    if (!clientId.trim() || !secret.trim()) {
      toast.error("Enter both Client ID and Secret");
      return;
    }
    setSaving(true);
    try {
      await api.saveTwitch(clientId.trim(), secret.trim());
      toast.success("Twitch connected!");
      onSaved();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not verify credentials");
    }
    setSaving(false);
  };

  const copyRedirect = () => {
    navigator.clipboard.writeText(redirectUrl);
    setCopied(true);
    toast.success("Redirect URL copied");
    setTimeout(() => setCopied(false), 1500);
  };

  const saveBuffer = async () => {
    if (!bufferKey.trim()) {
      toast.error("Paste your Buffer API key");
      return;
    }
    setBufferSaving(true);
    try {
      await api.saveBuffer(bufferKey.trim());
      toast.success("Buffer connected!");
      setBufferKey("");
      onSaved();
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not save Buffer key");
    }
    setBufferSaving(false);
  };

  const fetchBufferChannels = async () => {
    setBufferFetching(true);
    try {
      const chans = await api.getBufferChannels();
      setBufferChannels(chans);
      if (chans.length === 0) {
        toast.info("No connected channels found on your Buffer account yet.");
      } else {
        toast.success(`Found ${chans.length} connected channel${chans.length !== 1 ? "s" : ""}`);
      }
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not fetch Buffer channels");
    }
    setBufferFetching(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-[#12121A] border-[#262636] text-white max-w-lg max-h-[85vh] overflow-y-auto" data-testid="settings-dialog">
        <DialogHeader>
          <DialogTitle className="font-outfit text-xl">Connect Twitch</DialogTitle>
          <DialogDescription className="text-[#A0A0B8]">
            Free, takes ~2 minutes. Your keys stay on the server.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <ol className="text-sm text-[#A0A0B8] space-y-1.5 list-decimal pl-4">
            <li>
              Open the{" "}
              <a
                href="https://dev.twitch.tv/console/apps/create"
                target="_blank"
                rel="noreferrer"
                className="text-[#9146FF] hover:underline inline-flex items-center gap-1"
              >
                Twitch Dev Console <ExternalLink className="h-3 w-3" />
              </a>{" "}
              and register an app.
            </li>
            <li>Paste this exact OAuth Redirect URL:</li>
          </ol>

          <div className="flex items-center gap-2 rounded-lg bg-[#0B0B12] border border-[#262636] p-2">
            <code className="flex-1 text-[11px] font-jb text-[#00F0FF] break-all" data-testid="twitch-redirect-url">
              {redirectUrl}
            </code>
            <Button onClick={copyRedirect} variant="outline" className="h-8 w-8 p-0 bg-transparent border-[#262636] shrink-0">
              {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            </Button>
          </div>

          <ol className="text-sm text-[#A0A0B8] space-y-1.5 list-decimal pl-4" start={3}>
            <li>Category: Application Integration. Then copy your Client ID and generate a Secret.</li>
          </ol>

          <div className="space-y-3 pt-1">
            <div>
              <Label className="text-xs text-[#A0A0B8]">Client ID</Label>
              <Input
                data-testid="twitch-client-id-input"
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                placeholder={settings?.twitch_configured ? "•••••• (already set — re-enter to change)" : "your client id"}
                className="bg-[#0B0B12] border-[#262636] text-white mt-1 focus-visible:ring-[#9146FF]"
              />
            </div>
            <div>
              <Label className="text-xs text-[#A0A0B8]">Client Secret</Label>
              <Input
                data-testid="twitch-client-secret-input"
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder="your client secret"
                className="bg-[#0B0B12] border-[#262636] text-white mt-1 focus-visible:ring-[#9146FF]"
              />
            </div>
          </div>

          <Button
            data-testid="save-twitch-button"
            onClick={save}
            disabled={saving}
            className="w-full bg-[#9146FF] hover:bg-[#772CE8] text-white font-semibold"
          >
            {saving ? "Verifying…" : "Save & Connect"}
          </Button>

          {settings?.twitch_configured && (
            <div className="pt-2 border-t border-[#262636] space-y-2">
              <p className="text-xs text-[#A0A0B8]">
                Optional: authorize your account to auto-create clips on live streams.
              </p>
              <Button
                onClick={startOAuth}
                variant="outline"
                className="w-full bg-transparent border-[#9146FF]/40 text-[#9146FF] hover:bg-[#9146FF]/10"
                data-testid="connect-account-button"
              >
                {settings?.oauth_connected ? "Account connected ✓ — reconnect" : "Authorize clip creation"}
              </Button>
            </div>
          )}

          {/* ---------------- Buffer (auto-posting) ---------------- */}
          <div className="pt-4 border-t border-[#262636] space-y-3">
            <div>
              <h3 className="font-outfit font-bold text-white text-sm">Connect Buffer</h3>
              <p className="text-xs text-[#A0A0B8] mt-0.5">
                Lets StreamClip AI post finished clips straight to TikTok, Instagram, or
                YouTube for you. Get your key from{" "}
                <a
                  href="https://publish.buffer.com/settings/api"
                  target="_blank"
                  rel="noreferrer"
                  className="text-[#9146FF] hover:underline inline-flex items-center gap-1"
                >
                  Buffer → Settings → API <ExternalLink className="h-3 w-3" />
                </a>
                .
              </p>
            </div>

            <div className="flex items-center gap-2">
              <Input
                data-testid="buffer-api-key-input"
                type="password"
                value={bufferKey}
                onChange={(e) => setBufferKey(e.target.value)}
                placeholder={settings?.buffer_configured ? "•••••• (already set — re-enter to change)" : "your Buffer API key"}
                className="bg-[#0B0B12] border-[#262636] text-white focus-visible:ring-[#9146FF]"
              />
              <Button
                data-testid="save-buffer-button"
                onClick={saveBuffer}
                disabled={bufferSaving}
                className="bg-[#9146FF] hover:bg-[#772CE8] text-white font-semibold shrink-0"
              >
                {bufferSaving ? "Saving…" : "Save"}
              </Button>
            </div>

            {settings?.buffer_configured && (
              <div className="space-y-2">
                <Button
                  data-testid="fetch-buffer-channels-button"
                  onClick={fetchBufferChannels}
                  disabled={bufferFetching}
                  variant="outline"
                  className="w-full bg-transparent border-[#262636] text-white hover:bg-[#1A1A26]"
                >
                  <RefreshCw className={`h-4 w-4 mr-2 ${bufferFetching ? "animate-spin" : ""}`} />
                  {bufferFetching ? "Fetching…" : "Fetch my connected accounts"}
                </Button>

                {bufferChannels.length > 0 && (
                  <div className="rounded-lg bg-[#0B0B12] border border-[#262636] p-2 space-y-1 max-h-40 overflow-y-auto">
                    {bufferChannels.map((c) => (
                      <div
                        key={c.channel_id}
                        className="flex items-center justify-between text-xs font-jb text-[#A0A0B8] px-1 py-1"
                      >
                        <span className="truncate">{c.name || c.channel_id}</span>
                        <span className="text-[10px] uppercase text-[#00F0FF] shrink-0 ml-2">
                          {c.platform}
                        </span>
                      </div>
                    ))}
                    <p className="text-[10px] text-[#686880] pt-1 px-1">
                      Go to a channel's card on the dashboard to choose which of these it posts to.
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}