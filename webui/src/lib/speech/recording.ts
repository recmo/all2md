import { parseDocument } from 'yaml';

export type Range = { start: number; end: number; track?: string };
export type Attendee = { handle: string; identity: string; ranges?: Range[] };
export type Guidance = {
  mdstore?: string;
  audio?: string;
  manifest?: string;
  hotwords?: string[];
  attendees?: Attendee[];
  edits?: (Range & { before: string; after: string })[];
};
export function properties(text: string): Guidance {
  const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text);
  if (!match) return {};
  const document = parseDocument(match[1]);
  if (document.errors.length) throw Error(document.errors[0].message);
  return document.toJS({ maxAliasCount: 100 }) || {};
}
export function isRecording(text: string): boolean {
  try {
    return properties(text).mdstore === 'recording';
  } catch {
    return false;
  }
}
export function sibling(path: string, name: string): string {
  if (
    name.startsWith('/') ||
    name.split('/').some((part) => !part || part === '.' || part === '..')
  )
    throw Error('Source must be inside the recording directory');
  return path.slice(0, path.lastIndexOf('/') + 1) + name;
}
export type Turn = Range & { speaker: string; text: string };
export function turns(text: string): Turn[] {
  const result: Turn[] = [];
  for (const line of text.split('\n')) {
    const match =
      /^\*\*\[(\d{2}):(\d{2}):(\d{2}\.\d{2})\] (.+?):\*\*\s?(.*)$/.exec(line);
    if (!match) continue;
    const start =
      Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
    const markers = [...match[5].matchAll(/<!--\s*(\d+\.\d{2})s\s*-->/g)];
    if (
      !markers.length ||
      markers.at(-1)!.index! + markers.at(-1)![0].length !==
        match[5].trimEnd().length
    )
      throw Error('Transcript needs regeneration: missing final timing marker');
    const offsets = markers.map((marker) => Number(marker[1]));
    if (offsets.some((value, i) => value <= (offsets[i - 1] || 0)))
      throw Error('Invalid transcript timing');
    result.push({
      start,
      end: start + offsets.at(-1)!,
      speaker: match[4],
      text: match[5].replace(/<!--\s*\d+\.\d{2}s\s*-->/g, '').trim()
    });
  }
  return result;
}
