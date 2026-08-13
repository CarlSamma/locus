import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type HackA0Entry } from "../api";
import { EmptyState, KpiCard, Panel, Spinner } from "../components/ui";

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("it-IT", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function TweetCell({ text }: { text: string | null }) {
  return (
    <div className="font-body text-sm text-textPrimary whitespace-pre-wrap break-words max-w-md">
      {text || <span className="text-textSecondary/40 italic">—</span>}
    </div>
  );
}

function EntryRow({ e }: { e: HackA0Entry }) {
  return (
    <tr className="border-b border-white/5 hover:bg-white/5 align-top">
      <td className="px-4 py-3 font-mono text-[11px] text-textSecondary whitespace-nowrap">{fmtDate(e.created_at)}</td>
      <td className="px-3 py-3 text-center">
        <span
          className={`px-2 py-0.5 rounded-full border text-[10px] uppercase font-bold tracking-wider font-mono ${
            e.is_reply ? "bg-tertiary/10 text-tertiary border-tertiary/20" : "bg-white/5 text-textSecondary border-white/10"
          }`}
        >
          {e.is_reply ? "reply" : "post"}
        </span>
      </td>
      <td className="px-3 py-3">
        <div className="font-label text-[11px] text-secondary uppercase tracking-wider mb-1">Domanda</div>
        <TweetCell text={e.question_text} />
      </td>
      <td className="px-3 py-3">
        <div className="font-label text-[11px] text-primary uppercase tracking-wider mb-1">Risposta @HackingA0</div>
        <TweetCell text={e.text} />
        {e.question_user_handle && (
          <div className="mt-1 font-mono text-[10px] text-textSecondary">da @{e.question_user_handle}</div>
        )}
      </td>
    </tr>
  );
}

export default function HackingA0Archive() {
  const [kind, setKind] = useState("all");
  const [hasQuestion, setHasQuestion] = useState(false);
  const [limit, setLimit] = useState(50);
  const [search, setSearch] = useState("");
  const [submitted, setSubmitted] = useState("");

  const stats = useQuery({ queryKey: ["hacka0-stats"], queryFn: api.hacka0Stats });
  const qa = useQuery({
    queryKey: ["hacka0-qa", kind, hasQuestion, limit],
    queryFn: () => api.hacka0QA({ kind, has_question: hasQuestion, limit }),
  });
  const searchRes = useQuery({
    queryKey: ["hacka0-search", submitted],
    queryFn: () => api.hacka0Search(submitted, 100),
    enabled: submitted.length > 0,
  });

  const showSearch = submitted.trim().length > 0;
  const items = showSearch ? (searchRes.data ?? []) : (qa.data?.items ?? []);
  const total = showSearch ? items.length : qa.data?.total ?? 0;

  return (
    <div className="space-y-6">
      {/* Statistiche */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KpiCard label="Tweet @HackingA0" value={String(stats.data?.total ?? "—")} accent />
        <KpiCard label="Risposte (reply)" value={String(stats.data?.replies ?? "—")} />
        <KpiCard label="Con domanda" value={String(stats.data?.with_question ?? "—")} />
        <KpiCard label="Finestra" value={stats.data?.earliest ? `${fmtDate(stats.data.earliest)} – ${fmtDate(stats.data.latest)}` : "—"} />
      </div>

      {/* Filtri + ricerca */}
      <Panel title="Filtri">
        <div className="p-5 flex flex-wrap items-end gap-4">
          <label className="flex flex-col gap-1 text-xs text-textSecondary">
            Tipo
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              className="bg-input border border-white/10 rounded px-3 py-2 font-mono text-sm text-textPrimary"
            >
              <option value="all">tutti</option>
              <option value="reply">solo reply</option>
              <option value="post">solo post</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-xs text-textSecondary pb-2">
            <input
              type="checkbox"
              checked={hasQuestion}
              onChange={(e) => setHasQuestion(e.target.checked)}
              className="accent-primary"
            />
            Solo con domanda
          </label>
          <label className="flex flex-col gap-1 text-xs text-textSecondary">
            Limite
            <input
              type="number"
              value={limit}
              min={1}
              max={500}
              onChange={(e) => setLimit(Number(e.target.value) || 50)}
              className="w-24 bg-input border border-white/10 rounded px-3 py-2 font-mono text-sm text-textPrimary"
            />
          </label>
          <form
            className="flex-1 min-w-[220px] flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              setSubmitted(search.trim());
            }}
          >
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Cerca nel testo (risposta o domanda)…"
              className="flex-1 bg-input border border-white/10 rounded px-3 py-2 font-mono text-sm text-textPrimary focus:border-primary/50 outline-none"
            />
            <button
              type="submit"
              className="px-4 rounded bg-primary text-console text-sm font-bold hover:shadow-[0_0_15px_rgba(79,209,197,0.4)]"
            >
              Cerca
            </button>
          </form>
        </div>
      </Panel>

      {/* Tabella Q→A */}
      <Panel title={`Q→A Archive — ${total} righe`}>
        {showSearch ? (
          <div className="px-5 pt-4 font-mono text-[11px] text-textSecondary">
            Ricerca: <span className="text-primary">"{submitted}"</span>{" "}
            <button className="ml-2 text-warning underline" onClick={() => { setSubmitted(""); setSearch(""); }}>
              [x] clear
            </button>
          </div>
        ) : null}
        {qa.isLoading || searchRes.isLoading ? (
          <Spinner label="caricamento…" />
        ) : items.length === 0 ? (
          <EmptyState message="Nessuna voce nell'archivio. Avvia la harvest (scripts/harvest_hackinga0.py --fetch)." />
        ) : (
          <div className="overflow-auto">
            <table className="w-full text-left">
              <thead className="bg-[#252d3d]">
                <tr className="font-label text-[11px] uppercase tracking-wider text-textSecondary">
                  <th className="px-4 py-3">Data</th>
                  <th className="px-3 py-3">Tipo</th>
                  <th className="px-3 py-3">Domanda</th>
                  <th className="px-3 py-3">Risposta</th>
                </tr>
              </thead>
              <tbody>
                {items.map((e) => (
                  <EntryRow key={e.id} e={e} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
