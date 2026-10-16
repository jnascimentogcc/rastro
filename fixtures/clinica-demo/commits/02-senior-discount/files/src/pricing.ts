export type Patient = { age: number };

export function priceFor(patient: Patient, basePrice: number): number {
  if (patient.age >= 65) {
    return basePrice * 0.8;
  }
  return basePrice;
}
