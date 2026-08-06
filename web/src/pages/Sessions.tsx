import { useQuery } from "@tanstack/react-query";
import { api, type SessionRecord } from "../api";
import { EmptyState, Spinner } from "../components/ui";

export default function Sessions() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });

  if (isLoading) return <Spinner label="Caricamento sessioni…" />;
  if (isError || !data) return <EmptyState message="Errore nel caricamento delle sessioni" />;

  const items: SessionRecord[] = Array.isArray(data) ? data : [];
  if (items.length === 0) return <EmptyState message="Nessuna sessione registrata" />;

  return (
    <div className="max-w-7xl mx-auto flex flex-col gap-6">
      <div className="bg-surface rounded-lg border border-white/5 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="bg-input text-xs font-label text-textSecondary uppercase tracking-wider border-b border-white/5">
                <th className="px-4 py-3 font-medium">Session ID</th>
                <th className="px-4 py-3 font-medium">Started</th>
                <th className="px-4 py-3 font-medium">Ended</th>
                <th className="px-4 py-3 font-medium text-right">Probes</th>
                <th className="px-4 py-3 font-medium">Target</th>
                <th className="px-4 py-3 font-medium">Status</th>
              </tr>
            </thead>
            <tbody className="font-mono text-sm divide-y divide-white/5">
              {items.map((s, i) => (
                <tr key={s.id} className={`hover:bg-white/[0.02] transition-colors ${i % 2 === 1 ? "bg-input/30" : ""}`}>
                  <td className="px-4 py-3 text-primary">{s.id}</td>
                  <td className="px-4 py-3 text-textSecondary">
                    {new Date(s.started_at).toLocaleString("it-IT")}
                  </td>
                  <td className="px-4 py-3 text-textSecondary">
                    {s.ended_at ? new Date(s.ended_at).toLocaleString("it-IT") : "—"}
                  </td>
                  <td className="px-4 py-3 text-right text-textSecondary">{s.probes_total}</td>
                  <td className="px-4 py-3 text-textSecondary">{s.target}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`px-2 py-0.5 rounded-full text-[10px] uppercase font-bold tracking-wider border font-mono ${
                        s.status === "done"
                          ? "bg-tertiary/10 text-tertiary border-tertiary/20"
                          : s.status === "running"
                            ? "bg-warning/10 text-warning border-warning/20"
                            : s.status === "paused"
                              ? "bg-secondary/10 text-secondary border-secondary/20"
                              : "bg-white/5 text-textSecondary border-white/10"
                      }`}
                    >
                      {s.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
