import { useEffect, useState, useCallback, useMemo } from "react";
import { motion } from "framer-motion";
import {
  Radio, Users, RefreshCw, Sparkles, Activity, Video, Rocket, Scissors, Menu,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import Sidebar from "@/components/Sidebar";
import ClipCard from "@/components/ClipCard";
import SettingsDialog from "@/components/SettingsDialog";
import PullVodDialog from "@/components/PullVodDialog";
import { api } from "@/lib/api";

const HYPE_TAGS = ["Hype Spike", "Laughter", "Creepy Moment", "Insane Play", "Clutch", "Fail", "Wholesome"];

function HypeMeter({ level }) {
  return (
    <div className="flex items-center gap-2">
      <Activity className="h-4 w-4 text-[#FF2A85]" />
      <div className="h-2 w-28 rounded-full bg-[#262636] overflow-hidden">
        <motion.div
          className="h-full rounded-full"
          style={{ background: "linear-gradient(90deg,#9146FF,#FF2A85)" }}
          initial={{ width: 0 }}
          animate={{ width: `${level}%` }}
          transition={{ duration: 0.6 }}
        />
      </div>
      <span className="text-xs font-jb text-[#FF2A85] font-semibold">{level}%</span>
    </div>
  );
}

function ChannelSection({ channel, live, hype, clips, onSync, onLimit, onSampleHype, onClipUpdated, refresh }) {
  const [syncing, setSyncing] = useState(false);
  const [sampling, setSampling] = useState(false);

  const doSync = async () => {
    setSyncing(true);
    try {
      const res = await onSync(channel.id);
      toast.success(`Grabbed ${res.stored} clips with AI titles`);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Sync failed");
    }
    setSyncing(false);
  };

  const doSample = async () => {
    setSampling(true);
    await onSampleHype(channel.id);
    setSampling(false);
  };

  return (
    <section className="mb-10" data-testid={`channel-tab-${channel.id}`}>
      <div className="flex flex-wrap items-center gap-4 mb-5">
        <div className="flex items-center gap-3">
          {channel.avatar_url ? (
            <img src={channel.avatar_url} alt="" className="h-11 w-11 rounded-full object-cover ring-2 ring-[#262636]" />
          ) : (
            <div className="h-11 w-11 rounded-full bg-[#262636] grid place-items-center font-bold text-white">
              {channel.display_name?.[0]}
            </div>
          )}
          <div>
            <div className="flex items-center gap-2">
              <h2 className="font-outfit font-bold text-white text-xl">{channel.display_name}</h2>
              {live?.is_live ? (
                <span
                  data-testid={`live-status-badge-${channel.id}`}
                  className="flex items-center gap-1 rounded-full bg-[#00E676]/15 text-[#00E676] text-[10px] font-jb font-bold px-2 py-0.5 uppercase tracking-wide"
                >
                  <span className="live-dot h-1.5 w-1.5 rounded-full bg-[#00E676]" /> Live
                </span>
              ) : (
                <span className="rounded-full bg-[#262636] text-[#686880] text-[10px] font-jb px-2 py-0.5 uppercase">
                  Offline
                </span>
              )}
            </div>
            {live?.is_live && (
              <p className="text-xs text-[#A0A0B8] flex items-center gap-1.5 mt-0.5">
                <Users className="h-3 w-3" />
                <span data-testid={`viewer-count-${channel.id}`}>{live.viewer_count?.toLocaleString()}</span>
                watching · <span className="truncate max-w-[200px]">{live.game_name}</span>
              </p>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 ml-auto">
          {hype != null && <HypeMeter level={hype} />}
          <div className="flex items-center gap-1.5 rounded-lg bg-[#12121A] border border-[#262636] px-2 h-9">
            <span className="text-[10px] font-jb text-[#686880] uppercase">Clips/day</span>
            <Input
              data-testid={`channel-clips-limit-input-${channel.id}`}
              type="number"
              min={1}
              max={100}
              value={channel.clips_per_day}
              onChange={(e) => onLimit(channel.id, parseInt(e.target.value || "1", 10))}
              className="h-7 w-14 bg-transparent border-0 text-white text-sm p-0 focus-visible:ring-0"
            />
          </div>
          {live?.is_live && !channel.is_demo && (
            <Button
              onClick={doSample}
              disabled={sampling}
              variant="outline"
              className="h-9 bg-transparent border-[#FF2A85]/40 text-[#FF2A85] hover:bg-[#FF2A85]/10 text-sm"
              data-testid={`sample-hype-button-${channel.id}`}
            >
              <Activity className={`h-4 w-4 mr-2 ${sampling ? "animate-pulse" : ""}`} /> Read chat
            </Button>
          )}
          {!channel.is_demo && <PullVodDialog channel={channel} onPulled={refresh} />}
          {!channel.is_demo && (
            <Button
              onClick={doSync}
              disabled={syncing}
              className="h-9 bg-[#9146FF] hover:bg-[#772CE8] text-white text-sm font-semibold"
              data-testid={`sync-button-${channel.id}`}
            >
              <RefreshCw className={`h-4 w-4 mr-2 ${syncing ? "animate-spin" : ""}`} /> Get clips
            </Button>
          )}
        </div>
      </div>

      {clips.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-[#262636] py-14 text-center">
          <Video className="h-8 w-8 text-[#686880] mx-auto mb-3" />
          <p className="text-[#A0A0B8] text-sm">No clips yet.</p>
          <p className="text-[#686880] text-xs mt-1">
            {channel.is_demo ? "Demo channel" : "Fetching & rendering the best clips automatically…"}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6">
          {clips.map((c, i) => (
            <ClipCard key={c.id} clip={c} index={i} onUpdated={onClipUpdated} />
          ))}
        </div>
      )}
    </section>
  );
}

export default function Dashboard() {
  const [settings, setSettings] = useState({});
  const [channels, setChannels] = useState([]);
  const [clips, setClips] = useState([]);
  const [liveMap, setLiveMap] = useState({});
  const [hypeMap, setHypeMap] = useState({});
  const [selectedId, setSelectedId] = useState("all");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [hypeFilter, setHypeFilter] = useState("all");
  const [seeding, setSeeding] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  const loadSettings = useCallback(async () => {
    const s = await api.getSettings();
    setSettings(s);
    return s;
  }, []);

  const loadChannels = useCallback(async () => {
    const chs = await api.listChannels();
    setChannels(chs);
    return chs;
  }, []);

  const loadClips = useCallback(async () => {
    const cs = await api.listClips();
    setClips(cs);
  }, []);

  const refreshLive = useCallback(async (chs) => {
    const list = chs || channels;
    const entries = await Promise.all(
      list.map(async (ch) => {
        try {
          return [ch.id, await api.live(ch.id)];
        } catch {
          return [ch.id, { is_live: false }];
        }
      })
    );
    setLiveMap(Object.fromEntries(entries));
  }, [channels]);

  useEffect(() => {
    (async () => {
      await loadSettings();
      const chs = await loadChannels();
      await loadClips();
      await refreshLive(chs);
    })();
    const poll = setInterval(() => { loadClips(); }, 6000);
    const params = new URLSearchParams(window.location.search);
    if (params.get("twitch") === "connected") {
      toast.success("Twitch account authorized for clip creation!");
      window.history.replaceState({}, "", "/");
    } else if (params.get("twitch") === "error") {
      toast.error("Twitch authorization failed. Try again.");
      window.history.replaceState({}, "", "/");
    }
    return () => clearInterval(poll);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const clipCounts = useMemo(() => {
    const m = {};
    clips.forEach((c) => { m[c.channel_id] = (m[c.channel_id] || 0) + 1; });
    return m;
  }, [clips]);

  const addChannel = async (url) => {
    try {
      const ch = await api.addChannel(url);
      toast.success(`Added ${ch.display_name}`);
      const chs = await loadChannels();
      await refreshLive(chs);
      setSelectedId(ch.id);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not add channel");
    }
  };

  const deleteChannel = async (id) => {
    await api.deleteChannel(id);
    toast.success("Channel removed");
    await loadChannels();
    await loadClips();
    if (selectedId === id) setSelectedId("all");
  };

  const setLimit = async (id, val) => {
    const clamped = Math.max(1, Math.min(100, val || 1));
    setChannels((prev) => prev.map((c) => (c.id === id ? { ...c, clips_per_day: clamped } : c)));
    await api.updateChannel(id, { clips_per_day: clamped });
  };

  const syncChannel = async (id) => {
    const res = await api.sync(id, 7);
    await loadClips();
    return res;
  };

  const sampleHype = async (id) => {
    try {
      const res = await api.hype(id);
      setHypeMap((m) => ({ ...m, [id]: res.hype_level }));
      toast.success(res.sampled ? `Chat: ${res.messages_per_minute} msgs/min` : "Chat is quiet right now");
    } catch {
      toast.error("Could not read chat");
    }
  };

  const onClipUpdated = (updated) => {
    setClips((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
  };

  const seedDemo = async () => {
    setSeeding(true);
    try {
      await api.seedDemo();
      toast.success("Demo channels loaded — explore away!");
      const chs = await loadChannels();
      await loadClips();
      await refreshLive(chs);
    } catch {
      toast.error("Could not load demo");
    }
    setSeeding(false);
  };

  const visibleChannels = selectedId === "all" ? channels : channels.filter((c) => c.id === selectedId);

  const clipsFor = (chId) => {
    let list = clips.filter((c) => c.channel_id === chId);
    if (hypeFilter !== "all") list = list.filter((c) => c.hype_type === hypeFilter);
    return list;
  };

  return (
    <div className="flex min-h-screen bg-[#08080C]">
      <Sidebar
        channels={channels}
        liveMap={liveMap}
        clipCounts={clipCounts}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onAdd={addChannel}
        onDelete={deleteChannel}
        onOpenSettings={() => setSettingsOpen(true)}
        twitchConfigured={settings.twitch_configured}
        isOpen={navOpen}
        onClose={() => setNavOpen(false)}
      />

      <main className="flex-1 min-w-0">
        <div className="sticky top-0 z-10 bg-[#08080C]/85 backdrop-blur-md border-b border-[#262636] px-6 lg:px-10 py-4 flex flex-wrap items-center gap-4">
          <button
            onClick={() => setNavOpen(true)}
            className="lg:hidden h-9 w-9 grid place-items-center rounded-lg bg-[#12121A] border border-[#262636] text-white"
            data-testid="mobile-nav-toggle"
          >
            <Menu className="h-5 w-5" />
          </button>
          <div>
            <h1 className="font-outfit font-extrabold text-white text-xl sm:text-2xl tracking-tight">Clip Studio</h1>
            <p className="text-xs text-[#686880] font-jb hidden sm:block">
              auto-clipping {channels.length} channel{channels.length !== 1 ? "s" : ""} at hype moments
            </p>
          </div>
          <div className="ml-auto flex items-center gap-3">
            <Select value={hypeFilter} onValueChange={setHypeFilter}>
              <SelectTrigger
                data-testid="filter-hype-moments-select"
                className="h-9 w-[140px] sm:w-[170px] bg-[#12121A] border-[#262636] text-white text-sm"
              >
                <SelectValue placeholder="Filter moments" />
              </SelectTrigger>
              <SelectContent className="bg-[#12121A] border-[#262636] text-white">
                <SelectItem value="all">All moments</SelectItem>
                {HYPE_TAGS.map((t) => (
                  <SelectItem key={t} value={t}>{t}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="px-6 lg:px-10 py-8">
          {channels.length === 0 ? (
            <div className="max-w-2xl mx-auto text-center py-16 float-in">
              <div className="h-16 w-16 rounded-2xl bg-[#9146FF] grid place-items-center mx-auto mb-6 shadow-[0_0_40px_rgba(145,70,255,0.5)]">
                <Scissors className="h-8 w-8 text-white" />
              </div>
              <h2 className="font-outfit font-extrabold text-white text-3xl sm:text-4xl mb-3">
                Turn live streams into <span className="text-[#9146FF]">viral clips</span>
              </h2>
              <p className="text-[#A0A0B8] mb-8 max-w-lg mx-auto">
                Add any Twitch channel — no login needed. StreamClip AI grabs its best hype, creepy and
                clutch moments, writes viral titles, hashtags and on-video captions, and hands you a
                ready 9:16 video to save to your phone.
              </p>
              <div className="flex items-center justify-center gap-3">
                <Button
                  onClick={seedDemo}
                  disabled={seeding}
                  data-testid="load-demo-button"
                  className="h-11 px-6 bg-[#9146FF] hover:bg-[#772CE8] text-white font-semibold"
                >
                  <Rocket className="h-4 w-4 mr-2" /> {seeding ? "Loading…" : "Load demo channels"}
                </Button>
                {!settings.twitch_configured && (
                  <Button
                    onClick={() => setSettingsOpen(true)}
                    variant="outline"
                    className="h-11 px-6 bg-transparent border-[#262636] text-white hover:bg-[#12121A]"
                  >
                    Connect Twitch
                  </Button>
                )}
              </div>
              {!settings.twitch_configured && (
                <p className="text-xs text-[#686880] mt-5 flex items-center justify-center gap-1.5">
                  <Sparkles className="h-3 w-3 text-[#9146FF]" />
                  Demo works instantly — connect Twitch for real live channels &amp; clips
                </p>
              )}
            </div>
          ) : (
            visibleChannels.map((ch) => (
              <ChannelSection
                key={ch.id}
                channel={ch}
                live={liveMap[ch.id]}
                hype={hypeMap[ch.id]}
                clips={clipsFor(ch.id)}
                onSync={syncChannel}
                onLimit={setLimit}
                onSampleHype={sampleHype}
                onClipUpdated={onClipUpdated}
                refresh={loadClips}
              />
            ))
          )}
        </div>
      </main>

      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        settings={settings}
        onSaved={async () => {
          await loadSettings();
          setSettingsOpen(false);
        }}
      />
    </div>
  );
}
