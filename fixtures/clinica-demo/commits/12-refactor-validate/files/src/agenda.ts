export type Slot = { day: number; hour: number; treatment: string };

export class AgendaService {
  validate(slot: Slot): boolean {
    if (this.isImplant(slot) && (this.isFridayAfternoon(slot) || this.isSaturday(slot))) {
      return false;
    }
    return true;
  }

  durationMinutes(treatment: string): number {
    if (treatment === 'orthodontics') {
      return 45;
    }
    return 30;
  }

  private isImplant(slot: Slot): boolean {
    return slot.treatment === 'implant';
  }

  private isFridayAfternoon(slot: Slot): boolean {
    return slot.day === 5 && slot.hour >= 12;
  }

  private isSaturday(slot: Slot): boolean {
    return slot.day === 6;
  }
}
