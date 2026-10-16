import type { Slot } from './agenda';

export class CalendarRules {
  isSlotAllowed(candidate: Slot): boolean {
    if (candidate.treatment === 'implant' && candidate.day === 5 && candidate.hour >= 12) {
      return false;
    }
    return true;
  }
}
