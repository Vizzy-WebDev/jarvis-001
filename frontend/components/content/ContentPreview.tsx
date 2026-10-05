'use client';

import { useState } from 'react';

import type { ContentItem, ContentMedia, ContentTypeInfo } from '@/lib/api-types';

import { asList } from './format';

/**
 * The actual content — not its title, not its caption: the video that will be
 * posted, the slides, the article's text. Supporting information is shown
 * beside this, never instead of it.
 *
 * Media is only ever put in <img>/<video>/<audio>. The server sends every file
 * as a forced download with a sandboxing CSP (an agent's upload is content
 * someone else wrote); media elements still play it, and nothing here ever
 * navigates to one or frames it.
 */
export function ContentPreview({
  item,
  info,
  fields,
  media,
}: {
  item: Pick<ContentItem, 'name' | 'contentType'>;
  info: ContentTypeInfo | undefined;
  /** Shown instead of the item's own — e.g. an earlier revision's. */
  fields: ContentItem['fields'];
  media: ContentMedia[];
}) {
  const render = info?.render ?? 'text';
  const primary = media.find((m) => m.role === 'primary');
  const thumbnail = media.find((m) => m.role === 'thumbnail' || m.role === 'cover');

  if (render === 'video') {
    return primary ? (
      <video
        data-testid="content-video"
        src={primary.url}
        poster={thumbnail?.kind === 'image' ? thumbnail.url : undefined}
        controls
        preload="metadata"
        className="max-h-[56vh] w-full rounded bg-black object-contain"
      />
    ) : <Missing what="video" />;
  }
  if (render === 'image') {
    return primary ? (
      // eslint-disable-next-line @next/next/no-img-element
      <img data-testid="content-image" src={primary.url} alt={item.name}
           className="max-h-[56vh] w-full rounded bg-black/40 object-contain" />
    ) : <Missing what="image" />;
  }
  if (render === 'slides') return <Slides slides={media.filter((m) => m.role === 'slide')} name={item.name} />;
  if (render === 'audio') {
    return (
      <div className="space-y-3">
        {thumbnail?.kind === 'image' && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={thumbnail.url} alt="" className="mx-auto max-h-56 rounded object-contain" />
        )}
        {primary ? <audio data-testid="content-audio" src={primary.url} controls className="w-full" />
          : <Missing what="audio" />}
      </div>
    );
  }
  // Text-shaped: a post, an article, a newsletter. Plain text, never rendered HTML.
  const heading = (fields.subject || fields.title) as string | undefined;
  const hashtags = asList(fields.hashtags);
  return (
    <article data-testid="content-text"
             className="max-h-[56vh] overflow-y-auto rounded border border-surface-border bg-surface/60 p-4">
      {heading && <h3 className="mb-2 text-[16px] font-semibold text-ink">{heading}</h3>}
      <p className="whitespace-pre-wrap break-words text-[14px] leading-relaxed text-ink">
        {(fields.body as string) || ''}
      </p>
      {hashtags.length > 0 && <p className="mt-3 text-[13px] text-accent">{hashtags.join(' ')}</p>}
    </article>
  );
}

function Slides({ slides, name }: { slides: ContentMedia[]; name: string }) {
  const [index, setIndex] = useState(0);
  if (!slides.length) return <Missing what="slides" />;
  const at = Math.min(index, slides.length - 1);
  const slide = slides[at]!;
  return (
    <div data-testid="content-slides">
      {slide.kind === 'video' ? (
        <video src={slide.url} controls className="max-h-[52vh] w-full rounded bg-black object-contain" />
      ) : (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={slide.url} alt={`${name} — slide ${at + 1}`}
             className="max-h-[52vh] w-full rounded bg-black/40 object-contain" />
      )}
      <div className="mt-2 flex items-center justify-center gap-3 text-[13px] text-ink-muted">
        <button type="button" className="rounded-pill px-3 py-1 hover:bg-white/[0.06] disabled:opacity-40"
                disabled={at === 0} onClick={() => setIndex(at - 1)} data-testid="slide-prev">‹ Previous</button>
        <span data-testid="slide-position">Slide {at + 1} of {slides.length}</span>
        <button type="button" className="rounded-pill px-3 py-1 hover:bg-white/[0.06] disabled:opacity-40"
                disabled={at === slides.length - 1} onClick={() => setIndex(at + 1)}
                data-testid="slide-next">Next ›</button>
      </div>
    </div>
  );
}

function Missing({ what }: { what: string }) {
  return (
    <div className="rounded border border-state-warn/30 bg-state-warn/[0.06] p-4 text-[13px] text-state-warn">
      The {what} itself is missing from this item.
    </div>
  );
}
