export function toIso(value, fieldName = 'time') {
  if (typeof value !== 'string' || value.trim() === '') {
    throw Object.assign(new Error(`${fieldName} must be an ISO 8601 string`), { status: 400, code: 'INVALID_TIME' });
  }
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) {
    throw Object.assign(new Error(`${fieldName} must be a valid ISO 8601 date-time`), { status: 400, code: 'INVALID_TIME' });
  }
  return new Date(ms).toISOString();
}

export function minutes(value, fieldName) {
  if (!Number.isInteger(value) || value <= 0) {
    throw Object.assign(new Error(`${fieldName} must be a positive integer number of minutes`), { status: 400, code: 'INVALID_DURATION' });
  }
  return value;
}

export function addMinutes(iso, amount) {
  return new Date(Date.parse(iso) + amount * 60_000).toISOString();
}

export function overlap(aStart, aEnd, bStart, bEnd) {
  return Date.parse(aStart) < Date.parse(bEnd) && Date.parse(bStart) < Date.parse(aEnd);
}

export function contains(outerStart, outerEnd, innerStart, innerEnd) {
  return Date.parse(outerStart) <= Date.parse(innerStart) && Date.parse(innerEnd) <= Date.parse(outerEnd);
}
