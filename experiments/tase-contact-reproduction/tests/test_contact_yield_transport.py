"""Recipe ownership and serialization checks with no controller endpoint."""
from types import SimpleNamespace
import pytest
import contact_yield_transport as native
from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, TransportError
from step5d_autotune_v4_r012.register_transport import R012LiveRTDETransport
from build_contact_benchmark_triplet import transform, SOURCE
from test_contact_benchmark_triplet import receipt


class Client:
    def __init__(self,*args,**kwargs):
        self.events=[]
    def __enter__(self):self.events.append('open');return self
    def __exit__(self,*args):self.events.append('close')
    def negotiate(self):pass
    def setup_outputs(self,hz,fields):
        assert hz==500. and fields==OUTPUT_FIELDS
        # Real output validation is covered separately; isolate input ownership.
        return 1,[]
    def setup_inputs(self,fields):
        self.fields=tuple(fields)
        return 2,['DOUBLE']*24+['INT32']*10
    def start(self):self.events.append('start')
    def send_input_sample(self,recipe,types,values):
        self.sent=dict(zip(self.fields,values,strict=True))


def test_native_recipe_leaves_onrobot_input24_and_serializes_logical_slot(monkeypatch):
    client=Client()
    monkeypatch.setattr(native,'WritableRTDEClient',lambda *a,**kw:client)
    monkeypatch.setattr(native,'_validate_output_recipe',lambda types:None)
    transport=native.NativeYieldRTDETransport('injected')
    transport.open()
    assert 'input_int_register_24' not in client.fields
    assert len(client.fields)==len(set(client.fields))==34
    transport.send_packet([.25]*24,[3,2,1,0,13,7,1,1,123])
    assert client.sent['input_int_register_36']==3
    assert client.sent['input_int_register_25']==2
    assert client.sent['input_int_register_28']==13
    assert client.sent['input_int_register_35']==0
    assert client.sent['input_double_register_24']==.25
    script=transform(SOURCE.read_text(),receipt(),'2026-09-20T1100Z_TEST')
    assert 'read_input_integer_register(24)' not in script
    assert script.count('read_input_integer_register(36)')==7
    assert 'read_input_float_register(24)' in script
    assert 'write_output_integer_register(24,' in script
    assert 'rtde_set_watchdog(' not in script
    transport.close()
    assert client.events==['open','start','close']


def test_invalid_recipe_closes_without_start(monkeypatch):
    client=Client()
    client.setup_inputs=lambda fields:(2,['IN_USE']*34)
    monkeypatch.setattr(native,'WritableRTDEClient',lambda *a,**kw:client)
    monkeypatch.setattr(native,'_validate_output_recipe',lambda types:None)
    transport=native.NativeYieldRTDETransport('injected')
    with pytest.raises(TransportError):transport.open()
    assert transport.client is None and client.events==['open','close']


def test_native_entry_rejects_old_physical_input24_transport():
    writer=SimpleNamespace(_controller_transport=R012LiveRTDETransport('injected'))
    with pytest.raises(TransportError,match='conflicts'):
        native.install_native_yield_transport(writer)


def test_probe_recipe_binds_only_duration_34_before_sequence_35(monkeypatch):
    client=Client()
    monkeypatch.setattr(native,'WritableRTDEClient',lambda *a,**kw:client)
    monkeypatch.setattr(native,'_validate_output_recipe',lambda types:None)
    transport=native.ContactRampProbeRTDETransport('injected',ramp_duration_s=3)
    transport.open()
    assert len(client.fields)==len(set(client.fields))==35
    assert client.fields[24]=='input_int_register_36'
    assert client.fields[33]=='input_int_register_34'
    assert client.fields[34]=='input_int_register_35'
    transport.write_input_integer_register(35,17)
    transport.send_packet([.5]*24,[0,0,0,0,0,1,0,1,23])
    assert client.sent['input_int_register_34']==3
    assert client.sent['input_int_register_35']==17
    assert 'input_int_register_34' not in native.NATIVE_INPUT_FIELDS
    transport.close()


@pytest.mark.parametrize('duration',[0,-1,1.5,True,9])
def test_probe_recipe_rejects_unapproved_duration(duration):
    with pytest.raises(TransportError,match='duration'):
        native.ContactRampProbeRTDETransport('injected',ramp_duration_s=duration)
