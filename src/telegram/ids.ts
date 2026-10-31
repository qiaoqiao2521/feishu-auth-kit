/** Refuse numbers that were already rounded by a caller's JSON parser. */
export function telegramId(value: unknown): string {
  if (typeof value === 'string' && /^-?(?:0|[1-9]\d*)$/.test(value)) return value;
  if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value);
  throw new TypeError('Telegram identifier must be an exact integer string or safe integer');
}

/** Preserve oversized JSON integer tokens before JSON.parse can round them. */
export function parseTelegramJson(text: string): unknown {
  let normalized = '';
  let i = 0;
  while (i < text.length) {
    if (text[i] === '"') {
      const start = i++;
      while (i < text.length) {
        if (text[i] === '\\') { i += 2; continue; }
        if (text[i++] === '"') break;
      }
      normalized += text.slice(start, i);
      continue;
    }
    if (text[i] === '-' || /[0-9]/.test(text[i])) {
      const number = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(text.slice(i));
      if (number) {
        const source = number[0];
        if (/^-?\d+$/.test(source) &&
            (BigInt(source) > BigInt(Number.MAX_SAFE_INTEGER) ||
             BigInt(source) < BigInt(Number.MIN_SAFE_INTEGER))) {
          normalized += JSON.stringify(source);
        } else normalized += source;
        i += source.length;
        continue;
      }
    }
    normalized += text[i++];
  }
  return JSON.parse(normalized);
}

/** Encode Telegram's Integer parameters without converting their IDs to Number. */
export function stringifyTelegramJson(value: unknown): string {
  if (typeof value === 'bigint') return value.toString();
  if (Array.isArray(value)) return `[${value.map(stringifyTelegramJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.entries(value).filter(([, item]) => item !== undefined)
      .map(([key, item]) => `${JSON.stringify(key)}:${stringifyTelegramJson(item)}`).join(',')}}`;
  }
  return JSON.stringify(value);
}
