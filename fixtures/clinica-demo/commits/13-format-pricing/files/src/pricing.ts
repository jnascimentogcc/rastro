export type Patient = {
  age: number;
  insured: boolean;
};

export function priceFor(patient: Patient, basePrice: number): number {
  if (patient.insured) {
    return basePrice * 0.15;
  }

  if (patient.age >= 65) {
    return basePrice * 0.8;
  }

  return basePrice;
}
