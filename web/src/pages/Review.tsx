import { useQuery } from "@tanstack/react-query";
import { api, type ReviewItem } from "../api";
import { EmptyState, Spinner } from "../components/ui";

export default function Review() {
  const { data, isLoading, isError } = useQuery({ queryKey: ["review"], queryFn: api.review });

  if (isLoading) return <Spinner label="Caricamento review…" />;
  if (isError || !data) return <EmptyState message="Errore nel caricamento della review" />;

  const items = Array.isArray(data) ? data : [];
  if (items.length === 0) return <EmptyState message="Nessun probe da revisionare" />;

  const maxScore = Math.max(...items.map((i) => i.score), 1);

  return (
    <div className="max-w-7xl mx-auto flex flex-col xl:flex-row gap-6">
      <div className="flex-1 flex flex-col gap-4">
        {items.map((item) => (
          <ReviewCard key={item.id} item={item} />
        ))}
      </div>
      <div className="w-full xl:w-[280px] flex-shrink-0">
        <ScoreDistribution items={items} maxScore={maxScore} />
      </div>
    </div>
  );
}

function ScoreDistribution({ items, maxScore }: { items: ReviewItem[]; maxScore: number }) {
  const buckets = Array.from({ length: 10 }, (_, i) => ({
    range: `${i}-${i + 1}`,
    count: items.filter((it) => it.score >= i && it.score < i + 1).length,
  }));
  return (
    <div className="bg-surface rounded-lg border border-white/5 p-5">
      <h2 className="font-headline font-bold text-sm text-textPrimary mb-4">Distribuzione score</h2>
      <div className="flex flex-col gap-2">
        {buckets.map((b) => (
          <div key={b.range} className="flex items-center gap-3">
            <span className="font-mono text-[10px] text-textSecondary w-8">{b.range}</span>
            <div className="flex-1 h-2 bg-input rounded-full overflow-hidden">
              <div
                className="h-full bg-primary"
                style={{ width: `${(b.count / maxScore) * 100}%` }}
              />
            </div>
            <span className="font-mono text-[10px] text-textSecondary w-4 text-right">{b.count}</span>
          </div>
        ))}
      </div>
      <div className="mt-4 pt-4 border-t border-white/5 flex items-center gap-4 font-mono text-[10px] text-textSecondary uppercase tracking-wider">
        <span className="text-tertiary">confirm → apply</span>
        <span className="text-error">deny → discard</span>
      </div>
    </div>
  );
}

function ReviewCard({ item }: { item: ReviewItem }) {
  let cls: Record<string, unknown> | null = null;
  try {
    if (item.classification) cls = JSON.parse(item.classification);
  } catch {
    cls = null;
  }
  return (
    <div className="bg-surface rounded-lg border border-white/5 p-5 flex flex-col gap-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-mono text-[10px] text-textSecondary">#{item.id}</span>
        <span className="font-mono text-[10px] text-primary px-1.5 py-0.5 bg-primary/10 rounded">
          [{item.property_key}]
        </span>
        <span className="font-mono text-[10px] text-textSecondary bg-white/5 px-1.5 py-0.5 rounded">
          {item.frame}
        </span>
        <span
          className={`ml-auto font-mono text-sm font-bold ${
            item.score >= 7 ? "text-tertiary" : item.score >= 4 ? "text-warning" : "text-textSecondary"
          }`}
        >
          {item.score}
        </span>
      </div>
      <div className="font-mono text-sm text-textPrimary/90">“{item.text}”</div>
      {item.reply_text && (
        <div className="bg-console rounded p-3 border border-white/5">
          <div className="font-label text-[10px] text-textSecondary uppercase tracking-wider mb-1">
            Risposta del target
          </div>
          <div className="font-mono text-xs text-tertiary/90">“{item.reply_text}”</div>
        </div>
      )}
      {cls && (
        <div className="flex flex-wrap gap-4 font-mono text-xs text-textSecondary">
          {Object.entries(cls).map(([k, v]) => (
            <span key={k}>
              <span className="text-primary">{k}</span>: {String(v)}
            </span>
          ))}
        </div>
      )}
      <div className="flex items-center gap-3 pt-1">
        <button className="bg-primary text-console px-4 py-1.5 rounded-lg font-headline font-bold text-xs hover:shadow-[0_0_15px_rgba(79,209,197,0.4)] transition-all">
          Approva
        </button>
        <button className="border border-white/15 text-textPrimary px-4 py-1.5 rounded-lg font-headline font-bold text-xs hover:bg-white/5 transition-all">
          Scarta
        </button>
      </div>
    </div>
  );
}
