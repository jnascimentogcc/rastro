export type Booking = { id: string; startsAt: Date };

export function cancel(booking: Booking, now: Date): boolean {
  const hoursAhead = (booking.startsAt.getTime() - now.getTime()) / 3_600_000;
  if (hoursAhead < 24) {
    return false;
  }
  return true;
}

export function canBook(existingToday: number): boolean {
  return existingToday < 3;
}
