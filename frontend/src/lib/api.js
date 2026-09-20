import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

const http = axios.create({ baseURL: API });

export const api = {
  getSettings: () => http.get("/settings").then((r) => r.data),
  saveTwitch: (client_id, client_secret) =>
    http.post("/settings/twitch", { client_id, client_secret }).then((r) => r.data),
  listChannels: () => http.get("/channels").then((r) => r.data),
  addChannel: (url) => http.post("/channels", { url }).then((r) => r.data),
  updateChannel: (id, body) => http.patch(`/channels/${id}`, body).then((r) => r.data),
  deleteChannel: (id) => http.delete(`/channels/${id}`).then((r) => r.data),
  live: (id) => http.get(`/channels/${id}/live`).then((r) => r.data),
  hype: (id) => http.get(`/channels/${id}/hype`).then((r) => r.data),
  sync: (id, period_days = 1) => http.post(`/channels/${id}/sync`, { period_days }).then((r) => r.data),
  pullVod: (id, days = 30) => http.post(`/channels/${id}/pull-vod?days=${days}`).then((r) => r.data),
  clipNow: (id) => http.post(`/channels/${id}/clip-now`).then((r) => r.data),
  listClips: (channelId) =>
    http.get("/clips", { params: channelId ? { channel_id: channelId } : {} }).then((r) => r.data),
  createDownloadJob: (id) => http.post(`/clips/${id}/download-jobs`).then((r) => r.data),
  getDownloadJob: (jobId) => http.get(`/download-jobs/${jobId}`).then((r) => r.data),
  regenerate: (id) => http.post(`/clips/${id}/generate`).then((r) => r.data),
  updateClip: (id, body) => http.patch(`/clips/${id}`, body).then((r) => r.data),
  deleteClip: (id) => http.delete(`/clips/${id}`).then((r) => r.data),
  seedDemo: () => http.post("/demo/seed").then((r) => r.data),
};

export const startOAuth = () => {
  window.location.href = `${API}/auth/twitch/start`;
};
