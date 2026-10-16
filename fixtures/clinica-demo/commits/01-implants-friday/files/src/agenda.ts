export type Slot = { day: number; hour: number; treatment: string };

export class AgendaService {
  validate(slot: Slot): boolean {
    if (slot.treatment === 'implant' && slot.day === 5 && slot.hour >= 12) {
      return false;
    }
    return true;
  }
}
