import { HistoryEventEntryType } from '@rotki/common';
import { describe, expect, it } from 'vitest';
import { emptyTronEventForm, toTronEventPayload, type TronEventFormState, tronEventSchema } from '@/modules/history/management/forms/tron-event-form';

const txRef = 'ab'.repeat(32);

function validState(): TronEventFormState {
  return {
    ...emptyTronEventForm('0'),
    address: 'TXM3pBpWRemyMFtpRsXCiXSgPJqmuZG4c4',
    amount: '1.5',
    asset: 'TRX',
    eventType: 'receive',
    txRef,
  };
}

/** Sorted, because the order zod reports issues in is not part of the contract. */
function issuePaths(state: TronEventFormState): string[] {
  const result = tronEventSchema(false, () => []).safeParse(state);
  if (result.success)
    return [];
  return result.error.issues.map(issue => issue.path.join('.')).sort();
}

describe('tronEventSchema', () => {
  it('should accept a filled form', () => {
    expect(issuePaths(validState())).toEqual([]);
  });

  it('should reject a hash or an address of another chain', () => {
    expect(issuePaths({ ...validState(), address: '0x5A0b54D5dc17e0AadC383d2db43B0a0D3E029c4c', txRef: `0x${txRef}` })).toEqual(['address', 'txRef']);
  });
});

describe('toTronEventPayload', () => {
  it('should send a tron event without a location, which the backend sets', () => {
    const payload = toTronEventPayload(validState());

    expect(payload.entryType).toBe(HistoryEventEntryType.TRON_EVENT);
    expect(payload).not.toHaveProperty('location');
  });
});
