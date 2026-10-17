const REMINDER_HOURS = 48;

export function reminderTime(startsAt: Date): Date {
  return new Date(startsAt.getTime() - REMINDER_HOURS * 3_600_000);
}
