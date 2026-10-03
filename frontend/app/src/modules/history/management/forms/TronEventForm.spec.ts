// Seam: what the TRON event form (rotkibv #11) saves. It sends a tron event to the shared history
// endpoint with the account and address the user enters, edits keep the identifier, and only a
// TRON transaction hash passes validation.
import type { TronEvent } from '@/modules/history/events/schemas';
import { bigNumberify, HistoryEventEntryType } from '@rotki/common';
import { createMock } from '@test/utils/create-mock';
import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, type Pinia, setActivePinia } from 'pinia';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAssetInfoApi } from '@/modules/assets/api/use-asset-info-api';
import { useAssetPricesApi } from '@/modules/assets/api/use-asset-prices-api';
import { setupDayjs } from '@/modules/core/common/data/date';
import { useLocations } from '@/modules/core/common/use-locations';
import { useSupportedChainsStore } from '@/modules/core/common/use-supported-chains-store';
import { useHistoryEventCounterpartyMappings } from '@/modules/history/events/mapping/use-history-event-counterparty-mappings';
import { useHistoryEvents } from '@/modules/history/events/use-history-events';
import TronEventForm from '@/modules/history/management/forms/TronEventForm.vue';
import AssetSelect from '@/modules/shell/components/inputs/AssetSelect.vue';

vi.mock('@/modules/history/events/use-history-events', () => ({
  useHistoryEvents: vi.fn(),
}));

vi.mock('@/modules/core/common/use-locations', () => ({
  useLocations: vi.fn(),
}));

vi.mock('@/modules/assets/api/use-asset-prices-api', () => ({
  useAssetPricesApi: vi.fn(),
}));

vi.mock('@/modules/history/events/mapping/use-history-event-counterparty-mappings', () => ({
  useHistoryEventCounterpartyMappings: vi.fn(),
}));

describe('forms/TronEventForm.vue', () => {
  let wrapper: VueWrapper<InstanceType<typeof TronEventForm>>;
  let addHistoryEventMock: ReturnType<typeof vi.fn<ReturnType<typeof useHistoryEvents>['addHistoryEvent']>>;
  let editHistoryEventMock: ReturnType<typeof vi.fn<ReturnType<typeof useHistoryEvents>['editHistoryEvent']>>;
  let pinia: Pinia;

  const event: TronEvent = {
    address: 'TXM3pBpWRemyMFtpRsXCiXSgPJqmuZG4c4',
    amount: bigNumberify(1.5),
    asset: 'TRX',
    counterparty: null,
    entryType: HistoryEventEntryType.TRON_EVENT,
    eventSubtype: 'none',
    eventType: 'receive',
    groupIdentifier: `tron_${'ab'.repeat(32)}`,
    identifier: 7,
    location: 'tron',
    locationLabel: 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t',
    sequenceIndex: 0,
    timestamp: 1700000000000,
    txRef: 'ab'.repeat(32),
    userNotes: 'Receive 1.5 TRX',
  };

  beforeAll(() => {
    setupDayjs();
    pinia = createPinia();
    setActivePinia(pinia);
  });

  beforeEach(() => {
    vi.useFakeTimers();
    addHistoryEventMock = vi.fn<ReturnType<typeof useHistoryEvents>['addHistoryEvent']>();
    editHistoryEventMock = vi.fn<ReturnType<typeof useHistoryEvents>['editHistoryEvent']>();
    vi.mocked(useAssetInfoApi().assetMapping).mockResolvedValue({ assetCollections: {}, assets: {} });
    vi.mocked(useLocations).mockReturnValue(createMock<ReturnType<typeof useLocations>>({
      tradeLocations: computed(() => [{ identifier: 'tron', name: 'TRON' }]),
    }));
    vi.mocked(useHistoryEvents).mockReturnValue(createMock<ReturnType<typeof useHistoryEvents>>({
      addHistoryEvent: addHistoryEventMock,
      editHistoryEvent: editHistoryEventMock,
    }));
    vi.mocked(useAssetPricesApi).mockReturnValue(createMock<ReturnType<typeof useAssetPricesApi>>({
      addHistoricalPrice: vi.fn<ReturnType<typeof useAssetPricesApi>['addHistoricalPrice']>(),
    }));
    vi.mocked(useHistoryEventCounterpartyMappings).mockReturnValue(createMock<ReturnType<typeof useHistoryEventCounterpartyMappings>>({
      counterparties: computed<string[]>(() => []),
    }));
  });

  afterEach(() => {
    wrapper.unmount();
    vi.useRealTimers();
  });

  function createWrapper(data: InstanceType<typeof TronEventForm>['$props']['data']): VueWrapper<InstanceType<typeof TronEventForm>> {
    return mount(TronEventForm, {
      global: { plugins: [pinia], stubs: { I18nT: true } },
      props: { data },
    });
  }

  it('should save an edited tron event with its identifier and the entered addresses', async () => {
    wrapper = createWrapper({ event, nextSequenceId: '1', type: 'edit' });
    await vi.advanceTimersToNextTimerAsync();
    await wrapper.find('[data-testid=location-label] input').setValue(event.address);
    await wrapper.find('[data-testid=address] input').setValue(event.locationLabel);
    await wrapper.find('[data-testid=notes] textarea:not([aria-hidden="true"])').setValue('my note');
    editHistoryEventMock.mockResolvedValueOnce({ success: true });

    expect(await wrapper.vm.save()).toBe(true);
    expect(editHistoryEventMock).toHaveBeenCalledWith(expect.objectContaining({
      address: event.locationLabel,
      entryType: HistoryEventEntryType.TRON_EVENT,
      identifier: event.identifier,
      locationLabel: event.address,
      txRef: event.txRef,
      userNotes: 'my note',
    }));
  });

  it('should not limit the asset search to the tron chain, where it finds no tron asset yet', async () => {
    useSupportedChainsStore().supportedChains = [{ id: 'tron', image: 'tron.svg', name: 'TRON', nativeToken: 'TRX', type: 'tron' }];
    wrapper = createWrapper({ nextSequenceId: '0', type: 'add' });
    await vi.advanceTimersToNextTimerAsync();

    expect(wrapper.findComponent(AssetSelect).props('source')).toEqual({ chain: undefined, showIgnored: true });
  });

  it('should not save a new event whose hash is not a tron transaction hash', async () => {
    wrapper = createWrapper({ nextSequenceId: '0', type: 'add' });
    await vi.advanceTimersToNextTimerAsync();
    await wrapper.find('[data-testid=tx-ref] input').setValue(`0x${event.txRef}`);

    expect(await wrapper.vm.save()).toBe(false);
    expect(wrapper.find('[data-testid=tx-ref]').text()).toContain('transactions.events.form.tx_hash.validation.valid');
  });
});
