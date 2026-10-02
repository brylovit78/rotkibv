import type { ExternalServiceKey } from '@/modules/integrations/types';
import type { useExternalApiKeys } from '@/modules/settings/api-keys/external/use-external-api-keys';
import { NotificationCategory, Severity } from '@rotki/common';
import { createMock } from '@test/utils/create-mock';
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createNotification } from '@/modules/core/notifications/notification-utils';
import { useNotificationsStore } from '@/modules/core/notifications/use-notifications-store';
import TronscanApiKey from '@/modules/settings/api-keys/external/TronscanApiKey.vue';

/**
 * The card promises its page three things: the key typed into the shared ServiceKey is saved
 * through useExternalApiKeys under the `tronscan` name; once that save succeeds, the pending
 * TronScan missing-key notification is gone, also one that arrived after the card was mounted;
 * and deleting goes through the shared confirmation, which is only offered for a saved key.
 */

const mockKey = ref<string>('');
const mockSave = vi.fn<(payload: ExternalServiceKey, postConfirmAction?: () => Promise<void> | void) => Promise<void>>();
const mockConfirmDelete = vi.fn<(name: string) => void>();

vi.mock('@/modules/settings/api-keys/external/use-external-api-keys', () => ({
  useExternalApiKeys: vi.fn(() => createMock<ReturnType<typeof useExternalApiKeys>>({
    actionStatus: () => computed(() => undefined),
    confirmDelete: mockConfirmDelete,
    loading: ref<boolean>(false),
    save: mockSave,
    useApiKey: () => computed<string>(() => get(mockKey)),
  })),
}));

/** The dialog chrome is not under test: the stub keeps the slots and its confirm button. */
const ServiceKeyCardStub = {
  emits: ['confirm'],
  template: '<div><slot name="left-buttons" /><slot /><button data-testid="confirm" @click="$emit(\'confirm\')" /></div>',
};

describe('modules/settings/api-keys/external/TronscanApiKey', () => {
  let wrapper: VueWrapper<InstanceType<typeof TronscanApiKey>>;

  function createWrapper(): VueWrapper<InstanceType<typeof TronscanApiKey>> {
    return mount(TronscanApiKey, {
      global: {
        stubs: { ServiceKeyCard: ServiceKeyCardStub },
      },
    });
  }

  beforeEach(() => {
    setActivePinia(createPinia());
    set(mockKey, '');
    mockSave.mockReset();
    mockSave.mockImplementation(async (_payload, postConfirmAction) => {
      await postConfirmAction?.();
    });
    mockConfirmDelete.mockReset();
  });

  afterEach(() => {
    wrapper?.unmount();
  });

  it('should save the typed key and clear a TronScan notification added after mounting', async () => {
    wrapper = createWrapper();
    const store = useNotificationsStore();
    const { data } = storeToRefs(store);
    store.add([NotificationCategory.TRONSCAN, NotificationCategory.ETHERSCAN].map(category => createNotification(
      store.getNextId(),
      { category, message: '', severity: Severity.WARNING, title: category },
    )));

    await wrapper.find('[data-testid=service-key__api-key] input').setValue('tronscan-key');
    await wrapper.find('[data-testid=confirm]').trigger('click');
    await flushPromises();

    expect(mockSave).toHaveBeenCalledWith({ apiKey: 'tronscan-key', name: 'tronscan' }, expect.any(Function));
    expect(get(data).map(notification => notification.category)).toEqual([NotificationCategory.ETHERSCAN]);
  });

  it('should only offer deletion for a saved key, through the shared confirmation', async () => {
    wrapper = createWrapper();
    expect(wrapper.find('[data-testid=delete-button]').attributes('disabled')).toBeDefined();

    set(mockKey, 'tronscan-key');
    await nextTick();
    await wrapper.find('[data-testid=delete-button]').trigger('click');

    expect(mockConfirmDelete).toHaveBeenCalledWith('tronscan');
  });
});
