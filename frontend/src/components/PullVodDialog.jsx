import { useState } from "react";
import { History } from "lucide-react";
import { toast } from "sonner";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { api } from "@/lib/api";

export default function PullVodDialog({ channel, onPulled }) {
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState("30");
  const [loading, setLoading] = useState(false);

  const pull = async () => {
    setLoading(true);
    try {
      const res = await api.pullVod(channel.id, parseInt(days, 10));
      toast.success(`Pulled ${res.stored} clips from the last ${days} days`);
      onPulled();
      setOpen(false);
    } catch (e) {
      toast.error(e?.response?.data?.detail || "Could not pull past clips");
    }
    setLoading(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          data-testid={`pull-vod-button-${channel.id}`}
          variant="outline"
          className="h-9 bg-transparent border-[#262636] text-[#A0A0B8] hover:text-white hover:bg-[#1A1A26] text-sm"
        >
          <History className="h-4 w-4 mr-2" /> Pull past broadcasts
        </Button>
      </DialogTrigger>
      <DialogContent className="bg-[#12121A] border-[#262636] text-white max-w-md">
        <DialogHeader>
          <DialogTitle className="font-outfit text-xl">Pull from past broadcasts</DialogTitle>
          <DialogDescription className="text-[#A0A0B8]">
            Grab the top-performing clips from {channel.display_name}'s past streams and auto-title them.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div>
            <label className="text-xs text-[#A0A0B8]">Look back window</label>
            <Select value={days} onValueChange={setDays}>
              <SelectTrigger className="mt-1 bg-[#0B0B12] border-[#262636] text-white">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#12121A] border-[#262636] text-white">
                <SelectItem value="7">Last 7 days</SelectItem>
                <SelectItem value="30">Last 30 days</SelectItem>
                <SelectItem value="90">Last 90 days</SelectItem>
                <SelectItem value="365">Last year</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button
            onClick={pull}
            disabled={loading}
            className="w-full bg-[#9146FF] hover:bg-[#772CE8] text-white font-semibold"
          >
            {loading ? "Scanning for best moments…" : "Pull clips"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
