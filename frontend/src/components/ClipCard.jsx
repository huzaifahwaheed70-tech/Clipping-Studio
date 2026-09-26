import { useState, useEffect } from "react";
import { motion } from "framer-motion";
import { Play, Copy, Sparkles, Clock, Check, SlidersHorizontal, Flame, Download, Loader2, Trash2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, API } from "@/lib/api";

const HYPE_COLOR = {
  "Hype Spike": "#FF2A85", Laughter: "#FFB703", "Creepy Moment": "#9146FF",
  "Insane Play": "#00F0FF", Clutch: "#00E676", Fail: "#FF6B6B", Wholesome: "#FF9EC4",
};
const POS_CLASS = { top: "top-3 items-start", center: "top-1/2 -translate-y-1/2 items-center", bottom: "bottom-3 items-end" };
const fmtTime = (s) => {
  const t = Math.max(0, Math.round(s || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(sec).padStart(2, "0")}`;
};

export default function ClipCard({ clip, onUpdated, onDeleted, index }) {
  const [busy, setBusy] = useState(false), [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false), [playing, setPlaying] = useState(false);
  const [sharing, setSharing] = useState(false), [deleting, setDeleting] = useState(false);
  const [ov, setOv] = useState(clip.caption_overlay || {}), [caption, setCaption] = useState(clip.ai_caption || "");
  const rendered = !clip.is_demo && clip.render_status === "done";
  const errored = clip.render_status === "error";
  const videoUrl = `${API}/clips/${clip.id}/video`;
  const thumbSrc = clip.is_demo ? clip.thumbnail_url : `${API}/clips/${clip.id}/thumb`;

  // Keep the card synchronized while the backend renders. The old UI started
  // rendering but never refreshed the card, so it could remain on Preparing forever.
  useEffect(() => {
    if (clip.is_demo || rendered) return undefined;
    let stopped = false;
    let timer = null;
    const poll = async () => {
      try {
        const all = await api.listClips();
        const fresh = all.find((c) => c.id === clip.id);
        if (fresh && !stopped) {
          onUpdated?.(fresh);
          if (fresh.render_status === "done" || fresh.render_status === "error") return;
        }
      } catch (_) {}
      if (!stopped) timer = setTimeout(poll, 2000);
    };
    timer = setTimeout(poll, 1200);
    return () => { stopped = true; if (timer) clearTimeout(timer); };
  }, [clip.id, clip.render_status, clip.is_demo, rendered, onUpdated]);

  useEffect(() => {
    setOv(clip.caption_overlay || {});
    setCaption(clip.ai_caption || "");
  }, [clip.id, clip.caption_overlay, clip.ai_caption]);

  const hypeColor = HYPE_COLOR[clip.hype_type] || "#9146FF";
  const copyAll = async () => {
    try { await navigator.clipboard.writeText(`${clip.ai_title}\n\n${(clip.ai_hashtags || []).join(" ")}`); } catch (_) {}
    setCopied(true); toast.success("Title + hashtags copied"); setTimeout(() => setCopied(false), 1600);
  };
  const regen = async () => {
    setBusy(true);
    try { const updated = await api.regenerate(clip.id); onUpdated?.(updated); setCaption(updated.ai_caption || ""); toast.success("AI copy regenerated"); }
    catch (e) { toast.error(e?.response?.data?.detail || e?.message || "Could not regenerate"); }
    finally { setBusy(false); }
  };
  const saveOverlay = async (next, nextCaption = caption) => {
    setOv(next);
    try { const updated = await api.updateClip(clip.id, { caption_overlay: next, ai_caption: nextCaption }); onUpdated?.(updated); }
    catch (e) { toast.error(e?.response?.data?.detail || "Could not save caption settings"); }
  };
  const play = () => {
    if (!rendered) { toast.info(errored ? "Rendering failed. Tap Retry." : "Still preparing this clip…"); return; }
    setPlaying(true);
  };
  const remove = async () => {
    setDeleting(true);
    try { await api.deleteClip(clip.id); toast.success("Clip deleted"); onDeleted?.(clip.id); }
    catch { toast.error("Could not delete clip"); setDeleting(false); }
  };
  const downloadBlob = (file) => {
    const url = URL.createObjectURL(file); const a = document.createElement("a");
    a.href = url; a.download = file.name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const save = async () => {
    setSharing(true); const tid = toast.loading("Getting your video…");
    try {
      const res = await fetch(videoUrl, { cache: "no-store" });
      if (!res.ok) throw new Error("Video is not ready yet. Please try again in a few seconds.");
      const blob = await res.blob();
      if (!blob.size) throw new Error("The rendered video is empty.");
      const fname = `${(clip.ai_title || clip.channel_login || "clip").replace(/[^a-zA-Z0-9]+/g, "_").slice(0, 60)}_9x16.mp4`;
      const file = new File([blob], fname, { type: "video/mp4" });
      if (navigator.canShare?.({ files: [file] })) {
        toast.dismiss(tid);
        try { await navigator.share({ files: [file], title: clip.ai_title }); toast.success("Video ready to save"); }
        catch (err) { if (!err || err.name !== "AbortError") downloadBlob(file); }
      } else { downloadBlob(file); toast.success("Saved to Downloads", { id: tid }); }
    } catch (e) { toast.error(e?.message || "Could not save", { id: tid }); }
    finally { setSharing(false); }
  };
  const prepare = async () => {
    toast.message("Rendering your 9:16 clip…");
    try {
      const status = await api.prepare(clip.id);
      onUpdated?.({ ...clip, render_status: status?.render_status === "done" ? "done" : "rendering", rendered: status?.render_status === "done" });
    } catch (e) { toast.error(e?.response?.data?.detail || e?.message || "Could not prepare clip"); }
  };

  return (
    <motion.div layout data-testid={`clip-card-${clip.id}`} className="float-in rounded-2xl bg-[#12121A] border border-[#262636] overflow-hidden hover:border-[#9146FF]/50 transition-colors group" style={{ animationDelay: `${(index % 6) * 55}ms` }}>
      <div className="relative aspect-video bg-black overflow-hidden">
        {playing && rendered ? (
          <video data-testid={`clip-video-player-${clip.id}`} src={videoUrl} className="w-full h-full object-contain bg-black" controls autoPlay playsInline preload="metadata" />
        ) : (
          <>
            <img src={thumbSrc} alt={clip.ai_title || clip.title || "Twitch clip"} className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-105" />
            <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-black/20" />
            <div className={`absolute left-0 right-0 flex justify-center px-4 pointer-events-none ${POS_CLASS[ov.position] || POS_CLASS.bottom}`}>
              <span className="font-outfit font-extrabold uppercase text-center leading-tight px-3 py-1 rounded-md" style={{ color: "#fff", fontSize: `${(ov.font_size || 28) / 2}px`, background: ov.bg || "rgba(8,8,12,0.55)", textShadow: ov.shadow ? "0 2px 8px rgba(0,0,0,0.9)" : "none", borderBottom: `3px solid ${ov.highlight || "#9146FF"}` }}>{caption || clip.ai_caption}</span>
            </div>
            <button data-testid={`clip-play-button-${clip.id}`} onClick={play} className="absolute inset-0 grid place-items-center">
              <span className="h-14 w-14 rounded-full bg-black/50 backdrop-blur grid place-items-center border border-white/20 group-hover:scale-110 group-hover:bg-[#9146FF] transition-all"><Play className="h-6 w-6 text-white ml-0.5" fill="white" /></span>
            </button>
            <div className="absolute top-3 left-3 flex items-center gap-1.5"><Badge className="text-[10px] font-jb font-semibold border-0 text-black gap-1" style={{ background: hypeColor }}><Flame className="h-3 w-3" /> {clip.hype_type}</Badge></div>
            <div className="absolute bottom-3 right-3 flex items-center gap-1 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-jb text-white"><Clock className="h-3 w-3" /> {Math.round(clip.duration || 0)}s</div>
          </>
        )}
      </div>
      <div className="p-4 space-y-3">
        <h3 className="font-outfit font-bold text-white text-sm leading-snug line-clamp-2">{clip.ai_title || clip.title}</h3>
        <div className="flex flex-wrap gap-1.5">{(clip.ai_hashtags || []).map((h, i) => <span key={i} className="text-[11px] font-jb text-[#00F0FF] bg-[#00F0FF]/10 px-1.5 py-0.5 rounded">{h}</span>)}</div>
        <div className="flex items-center gap-3 text-[11px] font-jb text-[#686880]"><span className="flex items-center gap-1"><Flame className="h-3 w-3" style={{ color: hypeColor }} />{clip.source_type === "live" ? "recorded live" : `from past broadcast @ ${fmtTime(clip.start_seconds)}`}</span>{clip.game_name && <span className="truncate">· {clip.game_name}</span>}</div>
        {editing && <div className="space-y-3 rounded-lg bg-[#0B0B12] border border-[#262636] p-3">
          <Input value={caption} onChange={(e) => setCaption(e.target.value)} onBlur={() => saveOverlay(ov, caption)} placeholder="On-video caption" className="bg-[#12121A] border-[#262636] text-white text-xs h-8" />
          <div className="flex items-center gap-2"><Select value={ov.position || "bottom"} onValueChange={(v) => saveOverlay({ ...ov, position: v })}><SelectTrigger className="h-8 text-xs bg-[#12121A] border-[#262636] text-white"><SelectValue /></SelectTrigger><SelectContent className="bg-[#12121A] border-[#262636] text-white"><SelectItem value="top">Top</SelectItem><SelectItem value="center">Center</SelectItem><SelectItem value="bottom">Bottom</SelectItem></SelectContent></Select><input type="color" value={ov.highlight || "#9146FF"} onChange={(e) => saveOverlay({ ...ov, highlight: e.target.value })} className="h-8 w-10 rounded bg-transparent border border-[#262636] cursor-pointer" /></div>
          <div className="flex items-center gap-2"><span className="text-[10px] font-jb text-[#686880] w-10">Size</span><Slider value={[ov.font_size || 28]} min={16} max={44} step={2} onValueChange={([v]) => setOv({ ...ov, font_size: v })} onValueCommit={([v]) => saveOverlay({ ...ov, font_size: v })} /></div>
        </div>}
        <div className="flex items-center gap-2 pt-1">
          <Button onClick={copyAll} variant="outline" className="h-9 w-9 p-0 bg-transparent border-[#262636] text-[#A0A0B8] hover:text-white hover:bg-[#1A1A26]">{copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}</Button>
          {clip.is_demo ? <Button onClick={() => toast.info("Sample clip — add your own channel for real clips")} className="flex-1 h-9 bg-[#262636] text-[#A0A0B8] text-xs font-semibold"><Download className="h-4 w-4 mr-1.5" /> Sample</Button> : rendered ? <Button onClick={save} disabled={sharing} className="flex-1 h-9 bg-[#00E676] hover:bg-[#00c765] text-black text-xs font-bold">{sharing ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> : <Download className="h-4 w-4 mr-1.5" />}{sharing ? "Saving…" : "Save video"}</Button> : errored ? <Button onClick={prepare} className="flex-1 h-9 bg-[#FF6B6B]/20 text-[#FF6B6B] text-xs font-semibold"><RefreshCw className="h-4 w-4 mr-1.5" /> Retry</Button> : <Button onClick={prepare} className="flex-1 h-9 bg-[#00E676]/20 text-[#00E676] text-xs font-semibold"><Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> Preparing…</Button>}
          <Button onClick={() => setEditing((e) => !e)} variant="outline" className="h-9 w-9 p-0 bg-transparent border-[#262636] text-[#A0A0B8]"><SlidersHorizontal className="h-4 w-4" /></Button>
          <Button onClick={regen} disabled={busy} variant="outline" className="h-9 w-9 p-0 bg-transparent border-[#262636] text-[#A0A0B8]"><Sparkles className={`h-4 w-4 ${busy ? "animate-pulse text-[#9146FF]" : ""}`} /></Button>
          <Button onClick={remove} disabled={deleting} variant="outline" className="h-9 w-9 p-0 bg-transparent border-[#262636] text-[#A0A0B8] hover:text-[#FF2A85]"><Trash2 className="h-4 w-4" /></Button>
        </div>
      </div>
    </motion.div>
  );
}
