import { useState, useRef } from "react";
import { motion } from "framer-motion";
import { Play, Copy, Sparkles, Eye, Clock, Check, SlidersHorizontal, Flame, Download, Loader2, Share2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { api, API } from "@/lib/api";

const HYPE_COLOR = {
  "Hype Spike": "#FF2A85",
  Laughter: "#FFB703",
  "Creepy Moment": "#9146FF",
  "Insane Play": "#00F0FF",
  Clutch: "#00E676",
  Fail: "#FF6B6B",
  Wholesome: "#FF9EC4",
};

const POS_CLASS = {
  top: "top-3 items-start",
  center: "top-1/2 -translate-y-1/2 items-center",
  bottom: "bottom-3 items-end",
};

export default function ClipCard({ clip, onUpdated, index }) {
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [phase, setPhase] = useState("idle"); // idle | rendering | ready
  const fileRef = useRef(null);
  const [ov, setOv] = useState(clip.caption_overlay || {});
  const [caption, setCaption] = useState(clip.ai_caption || "");

  const hypeColor = HYPE_COLOR[clip.hype_type] || "#9146FF";

  const copyAll = () => {
    const text = `${clip.ai_title}\n\n${(clip.ai_hashtags || []).join(" ")}`;
    navigator.clipboard.writeText(text);
    setCopied(true);
    toast.success("Title + hashtags copied");
    setTimeout(() => setCopied(false), 1600);
  };

  const regen = async () => {
    setBusy(true);
    try {
      const updated = await api.regenerate(clip.id);
      onUpdated(updated);
      setCaption(updated.ai_caption || "");
      toast.success("AI copy regenerated");
    } catch {
      toast.error("Could not regenerate");
    }
    setBusy(false);
  };

  const saveOverlay = async (next, nextCaption = caption) => {
    setOv(next);
    const updated = await api.updateClip(clip.id, { caption_overlay: next, ai_caption: nextCaption });
    onUpdated(updated);
  };

  const play = () => {
    if (clip.is_demo || !clip.embed_url) {
      toast.info("Sample clip — connect Twitch to load real playable clips");
      return;
    }
    setPlaying(true);
  };

  const runRender = async () => {
    if (clip.is_demo) {
      toast.info("Sample clip — add your own channel and hit 'Get clips' for real videos");
      return;
    }
    setPhase("rendering");
    const tid = toast.loading("Making your 9:16 clip… up to a minute");
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    try {
      const { job_id } = await api.createDownloadJob(clip.id);
      let status = "processing";
      let tries = 0;
      while (status === "processing" && tries < 150) {
        await sleep(2000);
        const s = await api.getDownloadJob(job_id);
        status = s.status;
        if (status === "error") throw new Error(s.error || "Render failed");
        tries += 1;
      }
      if (status !== "done") throw new Error("Rendering timed out — try a shorter clip");

      const res = await fetch(`${API}/download-jobs/${job_id}/file`);
      if (!res.ok) throw new Error("Rendered file was not ready");
      const blob = await res.blob();
      const fname = `${clip.channel_login || "clip"}_9x16.mp4`;
      fileRef.current = new File([blob], fname, { type: "video/mp4" });
      setPhase("ready");
      toast.success("Ready! Tap the green button to Save / Share", { id: tid });
    } catch (e) {
      setPhase("idle");
      toast.error(e?.response?.data?.detail || e.message || "Could not render", { id: tid });
    }
  };

  const shareOrSave = async () => {
    const file = fileRef.current;
    if (!file) {
      runRender();
      return;
    }
    const text = `${clip.ai_title}\n${(clip.ai_hashtags || []).join(" ")}`;
    // Native share sheet (mobile) — must run inside this tap
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({ files: [file], title: clip.ai_title, text });
        toast.success("Choose 'Save Video' to add it to your Photos");
        return;
      } catch (err) {
        if (err && err.name === "AbortError") return; // user cancelled
        // otherwise fall through to direct download
      }
    }
    // Desktop / unsupported fallback: direct download
    const url = URL.createObjectURL(file);
    const a = document.createElement("a");
    a.href = url;
    a.download = file.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast.success("Saved! Check your Downloads / Photos");
  };

  const onDownloadClick = () => {
    if (phase === "ready") shareOrSave();
    else if (phase === "idle") runRender();
  };

  const embedSrc = clip.embed_url
    ? `${clip.embed_url}&parent=${window.location.hostname}&autoplay=true`
    : "";

  return (
    <motion.div
      layout
      data-testid={`clip-card-${clip.id}`}
      className="float-in rounded-2xl bg-[#12121A] border border-[#262636] overflow-hidden hover:border-[#9146FF]/50 transition-colors group"
      style={{ animationDelay: `${(index % 6) * 55}ms` }}
    >
      <div className="relative aspect-video bg-black overflow-hidden">
        {playing && embedSrc ? (
          <iframe
            title={clip.title}
            src={embedSrc}
            className="w-full h-full"
            allowFullScreen
            allow="autoplay; fullscreen"
          />
        ) : (
          <>
            <img
              src={clip.thumbnail_url}
              alt={clip.title}
              className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105"
            />
            <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-black/20" />

            {/* caption overlay preview */}
            <div
              data-testid={`caption-overlay-preview-${clip.id}`}
              className={`absolute left-0 right-0 flex justify-center px-4 pointer-events-none ${POS_CLASS[ov.position] || POS_CLASS.bottom}`}
            >
              <span
                className="font-outfit font-extrabold uppercase text-center leading-tight px-3 py-1 rounded-md"
                style={{
                  color: "#fff",
                  fontSize: `${(ov.font_size || 28) / 2}px`,
                  background: ov.bg || "rgba(8,8,12,0.55)",
                  textShadow: ov.shadow ? "0 2px 8px rgba(0,0,0,0.9)" : "none",
                  borderBottom: `3px solid ${ov.highlight || "#9146FF"}`,
                }}
              >
                {caption || clip.ai_caption}
              </span>
            </div>

            <button
              data-testid={`clip-play-button-${clip.id}`}
              onClick={play}
              className="absolute inset-0 grid place-items-center"
            >
              <span className="h-14 w-14 rounded-full bg-black/50 backdrop-blur grid place-items-center border border-white/20 group-hover:scale-110 group-hover:bg-[#9146FF] transition-all">
                <Play className="h-6 w-6 text-white ml-0.5" fill="white" />
              </span>
            </button>

            <div className="absolute top-3 left-3 flex items-center gap-1.5">
              <Badge
                data-testid={`chat-hype-indicator-${clip.id}`}
                className="text-[10px] font-jb font-semibold border-0 text-black gap-1"
                style={{ background: hypeColor }}
              >
                <Flame className="h-3 w-3" /> {clip.hype_type}
              </Badge>
            </div>
            <div className="absolute bottom-3 right-3 flex items-center gap-1 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-jb text-white">
              <Clock className="h-3 w-3" /> {Math.round(clip.duration)}s
            </div>
          </>
        )}
      </div>

      <div className="p-4 space-y-3">
        <h3 className="font-outfit font-bold text-white text-sm leading-snug line-clamp-2">
          {clip.ai_title}
        </h3>

        <div className="flex flex-wrap gap-1.5">
          {(clip.ai_hashtags || []).map((h, i) => (
            <span key={i} className="text-[11px] font-jb text-[#00F0FF] bg-[#00F0FF]/10 px-1.5 py-0.5 rounded">
              {h}
            </span>
          ))}
        </div>

        <div className="flex items-center gap-3 text-[11px] font-jb text-[#686880]">
          <span className="flex items-center gap-1">
            <Eye className="h-3 w-3" /> {(clip.view_count || 0).toLocaleString()}
          </span>
          {clip.game_name && <span className="truncate">· {clip.game_name}</span>}
        </div>

        {editing && (
          <div className="space-y-3 rounded-lg bg-[#0B0B12] border border-[#262636] p-3">
            <Input
              value={caption}
              onChange={(e) => setCaption(e.target.value)}
              onBlur={() => saveOverlay(ov, caption)}
              placeholder="On-video caption"
              className="bg-[#12121A] border-[#262636] text-white text-xs h-8"
            />
            <div className="flex items-center gap-2">
              <Select value={ov.position || "bottom"} onValueChange={(v) => saveOverlay({ ...ov, position: v })}>
                <SelectTrigger className="h-8 text-xs bg-[#12121A] border-[#262636] text-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-[#12121A] border-[#262636] text-white">
                  <SelectItem value="top">Top</SelectItem>
                  <SelectItem value="center">Center</SelectItem>
                  <SelectItem value="bottom">Bottom</SelectItem>
                </SelectContent>
              </Select>
              <input
                type="color"
                value={ov.highlight || "#9146FF"}
                onChange={(e) => saveOverlay({ ...ov, highlight: e.target.value })}
                className="h-8 w-10 rounded bg-transparent border border-[#262636] cursor-pointer"
              />
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[10px] font-jb text-[#686880] w-10">Size</span>
              <Slider
                value={[ov.font_size || 28]}
                min={16}
                max={44}
                step={2}
                onValueChange={([v]) => setOv({ ...ov, font_size: v })}
                onValueCommit={([v]) => saveOverlay({ ...ov, font_size: v })}
              />
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 pt-1">
          <Button
            data-testid={`copy-title-hashtags-button-${clip.id}`}
            onClick={copyAll}
            className="flex-1 h-9 bg-[#9146FF] hover:bg-[#772CE8] text-white text-xs font-semibold"
          >
            {copied ? <Check className="h-4 w-4 mr-1.5" /> : <Copy className="h-4 w-4 mr-1.5" />}
            {copied ? "Copied" : "Copy for posting"}
          </Button>
          <Button
            data-testid={`download-clip-button-${clip.id}`}
            onClick={onDownloadClick}
            disabled={phase === "rendering"}
            variant={phase === "ready" ? "default" : "outline"}
            className={
              phase === "ready"
                ? "flex-1 h-9 bg-[#00E676] hover:bg-[#00c765] text-black text-xs font-bold animate-pulse"
                : "h-9 w-9 p-0 bg-transparent border-[#262636] text-[#A0A0B8] hover:text-[#00E676] hover:bg-[#1A1A26]"
            }
            title={phase === "ready" ? "Open your phone's Save / Share menu" : "Make 9:16 clip"}
          >
            {phase === "rendering" ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : phase === "ready" ? (
              <>
                <Share2 className="h-4 w-4 mr-1.5" /> Save / Share
              </>
            ) : (
              <Download className="h-4 w-4" />
            )}
          </Button>
          <Button
            data-testid={`caption-style-toggle`}
            onClick={() => setEditing((e) => !e)}
            variant="outline"
            className="h-9 w-9 p-0 bg-transparent border-[#262636] text-[#A0A0B8] hover:text-white hover:bg-[#1A1A26]"
            title="Customize caption"
          >
            <SlidersHorizontal className="h-4 w-4" />
          </Button>
          <Button
            onClick={regen}
            disabled={busy}
            variant="outline"
            className="h-9 w-9 p-0 bg-transparent border-[#262636] text-[#A0A0B8] hover:text-white hover:bg-[#1A1A26]"
            title="Regenerate AI copy"
          >
            <Sparkles className={`h-4 w-4 ${busy ? "animate-pulse text-[#9146FF]" : ""}`} />
          </Button>
        </div>
      </div>
    </motion.div>
  );
}
