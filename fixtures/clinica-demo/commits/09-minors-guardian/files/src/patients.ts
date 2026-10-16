export type NewPatient = { name: string; age: number; guardianName?: string };

export function validatePatient(patient: NewPatient): boolean {
  if (patient.age < 18 && !patient.guardianName) {
    return false;
  }
  return patient.name.length > 0;
}
