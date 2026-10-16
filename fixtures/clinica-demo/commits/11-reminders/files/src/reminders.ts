export function reminderTime(startsAt: Date): Date {
  return new Date(startsAt.getTime() - 48 * 3_600_000);
}
