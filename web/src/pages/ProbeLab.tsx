import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type Probe } from "../api";
import { Spinner } from "../components/ui";
type LogLine = { kind: "info" | "ok" | "err" | "warn"; text: string };

function useLog() {
  const [lines, setLines] = useState<LogLine[]>([]);
  const push = (kind: LogLine["kind"], text: string) => setLines((prev) => [...prev, { kind, text }]);
  return { lines, push, clear: () => setLines([]) };
}

export default function ProbeLab() {
  const queryClient = useQueryClient();
  const { lines, push, clear } = useLog();
  const logRef = useRef<HTMLDivElement>(null);

  const [propertyKey, setPropertyKey] = useState("");
  const [frameAlias, setFrameAlias] = useState("");
  const [maxProbes, setMaxProbes] = useState(5);
  const [dryRun, setDryRun] = useState(true);
  const [sessionId, setSessionId] = useState("");
  const [pollingSession, setPollingSession] = useState<string | null>(null);
  const [generated, setGenerated] = useState<Probe | null>(null);

  const { data: properties } = useQuery({ queryKey: ["properties"], queryFn: api.properties });
  const { data: frames } = useQuery({ queryKey: ["frames"], queryFn: api.frames });
  const { data: sessions, refetch: refetchSessions } = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });

  useEffect(() => {
    if (properties && properties.length > 0 && !propertyKey) {
      const sorted = [...properties].sort((a, b) => b.remaining_entropy - a.remaining_entropy);
      setPropertyKey(sorted[0].key);
    }
    if (frames && frames.length > 0 && !frameAlias) {
      setFrameAlias(frames[0].alias);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [properties, frames]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [lines]);

  useEffect(() => {
    if (!pollingSession) return;
    const t = setInterval(async () => {
      try {
        const info = await api.runStatus(pollingSession);
        if (info.status !== "running") {
          setPollingSession(null);
          push("ok", `[session] ${info.status} — ${pollingSession}`);
          await queryClient.invalidateQueries({ queryKey: ["status"] });
          refetchSessions();
        }
      } catch {
        setPollingSession(null);
      }
    }, 2000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollingSession]);

  const gen = useMutation({
    mutationFn: () => api.generateProbe({ property_key: propertyKey, frame_alias: frameAlias }),
    onSuccess: (probe) => {
      setGenerated(probe);
      push("info", `[generate] → ${probe.text}`);
    },
    onError: (e) => push("err", `[generate] ${e instanceof Error ? e.message : e}`),
  });

  const post = useMutation({
    mutationFn: () =>
      api.postProbe({ text: generated?.text ?? "", property_key: propertyKey, frame_alias: frameAlias, session_id: sessionId || undefined }),
    onSuccess: (res) => {
      push("ok", `[post] tweet_id=${res.tweet_id} → ${res.url}`);
      queryClient.invalidateQueries({ queryKey: ["status"] });
      queryClient.invalidateQueries({ queryKey: ["probes-recent"] });
    },
    onError: (e) => push("err", `[post] ${e instanceof Error ? e.message : e}`),
  });

  const run = useMutation({
    mutationFn: () =>
      api.run({ dry_run: dryRun, max_probes: maxProbes, session_id: sessionId || undefined }),
    onSuccess: (info) => {
      setSessionId(info.session_id);
      setPollingSession(info.session_id);
      push("ok", `[run] sessione ${info.session_id} avviata (dry_run=${info.dry_run})`);
      refetchSessions();
    },
    onError: (e) => push("err", `[run] ${e instanceof Error ? e.message : e}`),
  });

  const stop = useMutation({
    mutationFn: () => api.stopRun(sessionId),
    onSuccess: (res) => {
      setPollingSession(null);
      push("warn", `[stop] ${res.status}`);
    },
    onError: (e) => push("err", `[stop] ${e instanceof Error ? e.message : e}`),
  });

  const poll = useMutation({
    mutationFn: () => api.poll(),
    onSuccess: (res) => {
      const n = Array.isArray(res.replies) ? res.replies.length : 0;
      push(n > 0 ? "ok" : "warn", `[poll] ${n} risposte dal target`);
    },
    onError: (e) => push("err", `[poll] ${e instanceof Error ? e.message : e}`),
  });

  if (!properties || !frames) return <Spinner label="Caricamento Probe Lab…" />;

  return (
    <div className="max-w-7xl mx-auto flex flex-col xl:flex-row gap-6">
      <div className="flex-1 flex flex-col gap-6">
        <div className="bg-surface rounded-lg border border-white/5 p-5 flex flex-col gap-4">
          <h2 className="font-headline font-bold text-sm text-textPrimary flex items-center gap-2">
            <span className="material-symbols-outlined text-[18px] text-primary">science</span>
            Genera probe
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="font-label text-xs text-textSecondary uppercase tracking-wider">Proprietà</label>
              <select
                value={propertyKey}
                onChange={(e) => setPropertyKey(e.target.value)}
                className="mt-1 w-full bg-input border border-white/10 rounded-lg px-3 py-2 font-mono text-sm text-textPrimary focus:border-primary focus:outline-none"
              >
                {properties.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.key}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="font-label text-xs text-textSecondary uppercase tracking-wider">Frame</label>
              <select
                value={frameAlias}
                onChange={(e) => setFrameAlias(e.target.value)}
                className="mt-1 w-full bg-input border border-white/10 rounded-lg px-3 py-2 font-mono text-sm text-textPrimary focus:border-primary focus:outline-none"
              >
                {frames.map((f) => (
                  <option key={f.alias} value={f.alias}>
                    {f.alias} — {f.persona}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => gen.mutate()}
              disabled={gen.isPending}
              className="bg-primary text-console px-4 py-2 rounded-lg font-headline font-bold text-sm hover:shadow-[0_0_15px_rgba(79,209,197,0.4)] transition-all disabled:opacity-50"
            >
              {gen.isPending ? "Generazione…" : "Genera"}
            </button>
            <button
              onClick={() => post.mutate()}
              disabled={!generated || post.isPending}
              className="border border-white/15 text-textPrimary px-4 py-2 rounded-lg font-headline font-bold text-sm hover:bg-white/5 transition-all disabled:opacity-40"
            >
              Posta
            </button>
            <button
              onClick={() => poll.mutate()}
              disabled={poll.isPending}
              className="border border-white/15 text-textPrimary px-4 py-2 rounded-lg font-headline font-bold text-sm hover:bg-white/5 transition-all disabled:opacity-40"
            >
              Poll
            </button>
          </div>
          {generated && (
            <div className="bg-console border border-primary/20 rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                <span className="font-mono text-[10px] text-primary px-1.5 py-0.5 bg-primary/10 rounded">
                  [{generated.property_key}]
                </span>
                <span className="font-mono text-[10px] text-textSecondary bg-white/5 px-1.5 py-0.5 rounded">
                  {generated.frame}
                </span>
                <span className="ml-auto font-mono text-[10px] text-textSecondary">{generated.status}</span>
              </div>
              <p className="font-mono text-sm text-textPrimary/90 leading-relaxed">“{generated.text}”</p>
            </div>
          )}
        </div>

        <div className="bg-surface rounded-lg border border-white/5 p-5 flex flex-col gap-4">
          <h2 className="font-headline font-bold text-sm text-textPrimary flex items-center gap-2">
            <span className="material-symbols-outlined text-[18px] text-primary">play_circle</span>
            Controllo campagna
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-end">
            <div>
              <label className="font-label text-xs text-textSecondary uppercase tracking-wider">Session ID</label>
              <input
                value={sessionId}
                onChange={(e) => setSessionId(e.target.value)}
                placeholder="auto"
                className="mt-1 w-full bg-input border border-white/10 rounded-lg px-3 py-2 font-mono text-sm text-textPrimary focus:border-primary focus:outline-none"
              />
            </div>
            <div>
              <label className="font-label text-xs text-textSecondary uppercase tracking-wider">Max probes</label>
              <input
                type="number"
                min={1}
                value={maxProbes}
                onChange={(e) => setMaxProbes(Number(e.target.value))}
                className="mt-1 w-full bg-input border border-white/10 rounded-lg px-3 py-2 font-mono text-sm text-textPrimary focus:border-primary focus:outline-none"
              />
            </div>
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
                className="w-4 h-4 accent-[#4fd1c5]"
              />
              <span className="font-mono text-sm text-textSecondary">dry-run (offline)</span>
            </label>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => run.mutate()}
              disabled={run.isPending}
              className="bg-primary text-console px-5 py-2 rounded-lg font-headline font-bold text-sm hover:shadow-[0_0_15px_rgba(79,209,197,0.4)] transition-all disabled:opacity-50"
            >
              {run.isPending ? "Avvio…" : "Avvia sessione"}
            </button>
            <button
              onClick={() => stop.mutate()}
              disabled={!sessionId || stop.isPending}
              className="border border-error/40 text-error px-5 py-2 rounded-lg font-headline font-bold text-sm hover:bg-error/10 transition-all disabled:opacity-40"
            >
              Stop
            </button>
            {pollingSession && (
              <span className="font-mono text-[10px] text-warning uppercase tracking-wider animate-pulse">
                sessione in esecuzione…
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="w-full xl:w-[380px] flex-shrink-0 flex flex-col gap-6">
        <div className="bg-console border border-white/10 rounded-lg flex flex-col overflow-hidden">
          <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-transparent via-primary/30 to-transparent" />
          <div className="p-4 border-b border-white/5 flex items-center justify-between bg-surface/50">
            <h2 className="font-headline font-bold text-sm text-textPrimary flex items-center gap-2">
              <span className="material-symbols-outlined text-[18px] text-primary">terminal</span>
              Console
            </h2>
            <button
              onClick={clear}
              className="font-mono text-[10px] text-textSecondary hover:text-primary transition-colors uppercase"
            >
              clear
            </button>
          </div>
          <div ref={logRef} className="h-[360px] overflow-y-auto p-3 font-mono text-xs space-y-1">
            {lines.length === 0 && <div className="text-textSecondary/50">— nessun output —</div>}
            {lines.map((l, i) => (
              <div
                key={i}
                className={
                  l.kind === "ok"
                    ? "text-tertiary"
                    : l.kind === "err"
                      ? "text-error"
                      : l.kind === "warn"
                        ? "text-warning"
                        : "text-textSecondary"
                }
              >
                {l.text}
              </div>
            ))}
          </div>
        </div>

        <div className="bg-surface rounded-lg border border-white/5 p-4 flex flex-col gap-3">
          <h2 className="font-headline font-bold text-sm text-textPrimary flex items-center gap-2">
            <span className="material-symbols-outlined text-[18px] text-primary">history</span>
            Sessioni recenti
          </h2>
          {(!sessions || sessions.length === 0) && (
            <div className="font-mono text-xs text-textSecondary">Nessuna sessione</div>
          )}
          {sessions?.slice(0, 5).map((s) => (
            <div
              key={s.id}
              className="flex items-center justify-between bg-input/50 rounded px-3 py-2 border border-white/5"
            >
              <span className="font-mono text-[11px] text-textPrimary">{s.id.slice(0, 12)}…</span>
              <span
                className={`font-mono text-[10px] uppercase tracking-wider ${
                  s.status === "done"
                    ? "text-tertiary"
                    : s.status === "running"
                      ? "text-warning"
                      : "text-textSecondary"
                }`}
              >
                {s.status}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
