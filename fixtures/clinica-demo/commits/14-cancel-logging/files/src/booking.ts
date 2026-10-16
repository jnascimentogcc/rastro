export type Booking = { id: string; startsAt: Date };

export function cancel(booking: Booking, now: Date): boolean {
  try {
    const hoursAhead = (booking.startsAt.getTime() - now.getTime()) / 3_600_000;
    if (hoursAhead < 24) {
      return false;
    }
    return true;
  } catch (error) {
    console.error('cancel failed', error);
    return false;
  }
}

export function canBook(existingToday: number, emergency: boolean): boolean {
  if (emergency) {
    return true;
  }
  return existingToday < 3;
}
