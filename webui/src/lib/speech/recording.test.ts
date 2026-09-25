import { describe, expect, it } from 'vitest';
import { isRecording, properties, sibling, turns } from './recording';
import { applyAppEdit } from '../apps/apps';

describe('recording review', () => {
  it('uses explicit final timing markers, not the next speaker start', () => {
    const parsed = turns(
      '**[00:00:01.00] Alice:** First. <!-- 0.50s --> More. <!-- 2.00s -->\n**[00:00:01.50] Bob:** Overlap. <!-- 0.50s -->\n'
    );
    expect(parsed.map((turn) => [turn.start, turn.end])).toEqual([
      [1, 3],
      [1.5, 2]
    ]);
    expect(parsed[0].text).toBe('First.  More.');
    expect(() => turns('**[00:00:01.00] Alice:** Missing end')).toThrow(
      'timing'
    );
    expect(() =>
      turns('**[00:00:01.00] Alice:** Broken <!-- 2.00s --> <!-- 1.00s -->')
    ).toThrow('timing');
  });
  it('stages structured guidance without changing human notes', () => {
    const before =
      '---\nmdstore: recording\naudio: audio.m4a\n---\n# Meeting\n\nHuman notes.\n';
    const after = applyAppEdit(before, {
      path: 'recording.md',
      fields: {
        attendees: [
          {
            handle: 'Alice',
            identity: '',
            ranges: [{ track: 'mixed', start: 1, end: 2 }]
          }
        ]
      }
    });
    expect(isRecording(after)).toBe(true);
    expect(properties(after).attendees?.[0].handle).toBe('Alice');
    expect(after.split('---\n')[2]).toBe(before.split('---\n')[2]);
    expect(sibling('meetings/recording.md', 'audio.m4a')).toBe(
      'meetings/audio.m4a'
    );
    expect(() => sibling('meetings/recording.md', '../audio.m4a')).toThrow();
  });
});
