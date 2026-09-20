import { useState } from "react";
import { motion } from "framer-motion";
import { Plus, Scissors, Settings, Radio, Zap, Trash2, LayoutGrid } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function Sidebar({
  channels,
  liveMap,
  clipCounts,
  selectedId,
  onSelect,
  onAdd,
  onDelete,
  onOpenSettings,
  twitchConfigured,
  isOpen,
  onClose,
}) {
  const [url, setUrl] = useState("");
  const [adding, setAdding] = useState(false);

  const submit = async () => {
    if (!url.trim()) return;
    setAdding(true);
    await onAdd(url.trim());
    setUrl("");
    setAdding(false);
  };

  const select = (id) => {
    onSelect(id);
    onClose?.();
  };

  const totalClips = Object.values(clipCounts).reduce((a, b) => a + b, 0);

  return (
    <>
    {isOpen && <div onClick={onClose} className="fixed inset-0 bg-black/60 z-30 lg:hidden" />}
    <aside className={`w-[280px] shrink-0 h-screen fixed lg:sticky top-0 bg-[#0B0B12] border-r border-[#262636] flex flex-col z-40 transition-transform duration-300 lg:translate-x-0 ${isOpen ? "translate-x-0" : "-translate-x-full"}`}>
      <div className="p-5 border-b border-[#262636]">
        <div className="flex items-center gap-2.5">
          <div className="h-9 w-9 rounded-xl bg-[#9146FF] grid place-items-center shadow-[0_0_18px_rgba(145,70,255,0.5)]">
            <Scissors className="h-5 w-5 text-white" />
          </div>
          <div>
            <h1 className="font-outfit font-extrabold text-white text-lg leading-none tracking-tight">
              StreamClip<span className="text-[#9146FF]">AI</span>
            </h1>
            <p className="text-[10px] font-jb text-[#686880] mt-1 uppercase tracking-widest">clip engine</p>
          </div>
        </div>
      </div>

      <div className="p-4 border-b border-[#262636] space-y-2">
        <label className="text-[11px] font-jb uppercase tracking-wider text-[#686880]">Add channel</label>
        <div className="flex gap-2">
          <Input
            data-testid="add-channel-input"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="twitch.tv/username"
            className="bg-[#12121A] border-[#262636] text-white text-sm h-9 focus-visible:ring-[#9146FF]"
          />
          <Button
            data-testid="add-channel-button"
            onClick={submit}
            disabled={adding}
            className="h-9 w-9 p-0 bg-[#9146FF] hover:bg-[#772CE8] shrink-0"
          >
            <Plus className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <div className="px-3 py-2 flex-1 overflow-y-auto">
        <button
          data-testid="channel-tab-all"
          onClick={() => select("all")}
          className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg mb-1 transition-colors ${
            selectedId === "all" ? "bg-[#9146FF]/15 text-white" : "text-[#A0A0B8] hover:bg-[#12121A]"
          }`}
        >
          <LayoutGrid className="h-4 w-4" />
          <span className="text-sm font-medium flex-1 text-left">All Channels</span>
          <span className="text-[11px] font-jb text-[#686880]">{totalClips}</span>
        </button>

        {channels.map((ch) => {
          const live = liveMap[ch.id]?.is_live;
          return (
            <motion.div
              key={ch.id}
              layout
              data-testid={`channel-card-${ch.id}`}
              className={`group w-full flex items-center gap-3 px-3 py-2.5 rounded-lg mb-1 cursor-pointer transition-colors ${
                selectedId === ch.id ? "bg-[#9146FF]/15" : "hover:bg-[#12121A]"
              }`}
              onClick={() => select(ch.id)}
            >
              <div className="relative">
                {ch.avatar_url ? (
                  <img src={ch.avatar_url} alt={ch.display_name} className="h-8 w-8 rounded-full object-cover" />
                ) : (
                  <div className="h-8 w-8 rounded-full bg-[#262636] grid place-items-center text-xs font-bold text-white">
                    {ch.display_name?.[0]?.toUpperCase()}
                  </div>
                )}
                {live && (
                  <span
                    data-testid={`live-status-badge-${ch.id}`}
                    className="live-dot absolute -bottom-0.5 -right-0.5 h-3 w-3 rounded-full bg-[#00E676] border-2 border-[#0B0B12]"
                  />
                )}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-white truncate">{ch.display_name}</p>
                <p className="text-[11px] font-jb text-[#686880]">
                  {live ? "LIVE" : "offline"} · {clipCounts[ch.id] || 0} clips
                </p>
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); onDelete(ch.id); }}
                className="opacity-0 group-hover:opacity-100 text-[#686880] hover:text-[#FF2A85] transition-opacity"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </motion.div>
          );
        })}
      </div>

      <div className="p-4 border-t border-[#262636] space-y-2">
        <div className="flex items-center gap-2 text-[11px] font-jb">
          <span className="live-dot h-2 w-2 rounded-full bg-[#00E676]" />
          <span className="text-[#A0A0B8]">Auto-clipping · no login needed</span>
        </div>
        <Button
          data-testid="open-settings-button"
          onClick={onOpenSettings}
          variant="outline"
          className="w-full h-9 bg-transparent border-[#262636] text-[#A0A0B8] hover:text-white hover:bg-[#12121A] text-sm"
        >
          <Settings className="h-4 w-4 mr-2" /> Settings
        </Button>
      </div>
    </aside>
    </>
  );
}
