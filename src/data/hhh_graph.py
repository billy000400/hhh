import logging
import os.path as osp

import awkward as ak
import numpy as np
import torch
import uproot
import vector
from coffea.nanoevents import BaseSchema, NanoEventsFactory
from torch_geometric.data import Data, InMemoryDataset

vector.register_awkward()

logging.basicConfig(level=logging.INFO)

N_JETS = 10
MIN_JET_PT = 20
MIN_JETS = 6
N_HIGGS = 3
FEATURE_BRANCHES = ["jet{i}Pt", "jet{i}Eta", "jet{i}Phi", "jet{i}DeepFlavB", "jet{i}JetId"]
LABEL_BRANCHES = ["jet{i}HiggsMatchedIndex", "jet{i}HadronFlavour"]
ALL_BRANCHES = [branch.format(i=i) for i in range(1, N_JETS + 1) for branch in FEATURE_BRANCHES + LABEL_BRANCHES]


def get_n_features(name, events, n):
    return ak.concatenate([np.expand_dims(events[name.format(i=i)], axis=-1) for i in range(1, n + 1)], axis=-1)


def get_edge_index(arr):
    # single direction
    # return ak.argcombinations(arr, 2)
    # both directions
    edge_index = ak.argcartesian([arr, arr])
    one_index, two_index = ak.unzip(edge_index)
    mask_self_loops = one_index != two_index
    return edge_index[mask_self_loops]


def compute_edge_features(pt, eta, phi, higgs_idx, higgs_pt, higgs_eta, higgs_phi):
    jets = ak.zip(
        {"pt": pt, "eta": eta, "phi": phi, "mass": ak.zeros_like(pt), "higgs_idx": higgs_idx}, with_name="Momentum4D"
    )

    # single direction
    # jet_pairs = ak.combinations(jets, 2, fields=["j0", "j1"])
    # both directions
    jet_pairs = ak.cartesian({"j0": jets, "j1": jets})
    mask_self_loops = ~(jet_pairs.j0 == jet_pairs.j1)
    jet_pairs = jet_pairs[mask_self_loops]

    # in the future: can use higgses to check matching criteria
    higgses = ak.zip(  # noqa: F841
        {"pt": higgs_pt, "eta": higgs_eta, "phi": higgs_phi, "mass": ak.ones_like(higgs_pt) * 125.0},
        with_name="Momentum4D",
    )

    # helpers
    min_pt = ak.where(jet_pairs["j0"].pt < jet_pairs["j1"].pt, jet_pairs["j0"].pt, jet_pairs["j1"].pt)
    sum_pt = jet_pairs["j0"].pt + jet_pairs["j1"].pt

    # edge features
    log_delta_r = np.log(jet_pairs["j0"].deltaR(jet_pairs["j1"]))
    log_mass2 = np.log((jet_pairs["j0"] + jet_pairs["j1"]).mass2)
    log_kt = np.log(min_pt) + log_delta_r
    log_z = np.log(min_pt / sum_pt)
    log_pt_jj = np.log((jet_pairs["j0"] + jet_pairs["j1"]).pt)
    eta_jj = (jet_pairs["j0"] + jet_pairs["j1"]).eta
    phi_jj = (jet_pairs["j0"] + jet_pairs["j1"]).phi

    # edge targets
    edge_match = ak.where(jet_pairs["j0"].higgs_idx > 0, jet_pairs["j0"].higgs_idx == jet_pairs["j1"].higgs_idx, 0)
    return log_delta_r, log_mass2, log_kt, log_z, log_pt_jj, eta_jj, phi_jj, edge_match


class HHHGraph(InMemoryDataset):
    def __init__(self, root, transform=None, pre_transform=None, pre_filter=None, entry_start=None, entry_stop=None):
        self.raw_data = None
        self.entry_start = entry_start
        self.entry_stop = entry_stop
        super().__init__(root, transform, pre_transform, pre_filter)
        self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def raw_file_names(self):
        return [
            "GluGluToHHHTo6B_SM.root",
        ]

    @property
    def processed_file_names(self):
        return ["hhh_graph.pt"]

    def download(self):
        # Download to `self.raw_dir`.
        pass

    def process(self):
        # Read data into huge `Data` list.
        data_list = []

        for file_name in self.raw_file_names:
            in_file = uproot.open(osp.join(self.raw_dir, "..", "..", file_name))
            events = NanoEventsFactory.from_root(
                in_file, treepath="Events", entry_start=self.entry_start, entry_stop=self.entry_stop, schemaclass=BaseSchema
            ).events()

            higgs_pt = get_n_features("genHiggs{i}Pt", events, N_HIGGS)
            higgs_eta = get_n_features("genHiggs{i}Eta", events, N_HIGGS)
            higgs_phi = get_n_features("genHiggs{i}Phi", events, N_HIGGS)

            pt = get_n_features("jet{i}Pt", events, N_JETS)
            eta = get_n_features("jet{i}Eta", events, N_JETS)
            phi = get_n_features("jet{i}Phi", events, N_JETS)
            btag = get_n_features("jet{i}DeepFlavB", events, N_JETS)
            jet_id = get_n_features("jet{i}JetId", events, N_JETS)
            higgs_idx = get_n_features("jet{i}HiggsMatchedIndex", events, N_JETS)
            hadron_flavor = get_n_features("jet{i}HadronFlavour", events, N_JETS)

            # remove jets below MIN_JET_PT (i.e. zero-padded jets)
            mask = pt > MIN_JET_PT
            pt = pt[mask]
            eta = eta[mask]
            phi = phi[mask]
            btag = btag[mask]
            jet_id = jet_id[mask]
            higgs_idx = higgs_idx[mask]
            hadron_flavor = hadron_flavor[mask]

            # keep events with MIN_JETS jets
            mask = ak.num(pt) >= MIN_JETS
            pt = pt[mask]
            eta = eta[mask]
            phi = phi[mask]
            btag = btag[mask]
            jet_id = jet_id[mask]
            higgs_idx = higgs_idx[mask]
            hadron_flavor = hadron_flavor[mask]

            # switch -1 -> 0
            higgs_idx = ak.where(higgs_idx > -1, higgs_idx, 0)
            # require hadron_flavor == 5 (i.e. b-jet ghost association matching)
            higgs_idx = ak.where(hadron_flavor == 5, higgs_idx, 0)

            edge_indices = get_edge_index(ak.zeros_like(pt))
            log_delta_r, log_mass2, log_kt, log_z, log_pt_jj, eta_jj, phi_jj, edge_match = compute_edge_features(
                pt, eta, phi, higgs_idx, higgs_pt, higgs_eta, higgs_phi
            )

            n_events = len(pt)

            for i in range(0, n_events):
                if len(pt[i]) < MIN_JETS:
                    logging.warning(f"Less than {MIN_JETS} jets; skipping event")
                    continue
                # stack node feature vector
                x = torch.tensor(np.stack([np.log(pt[i]), eta[i], phi[i], btag[i], jet_id[i]], axis=-1))
                # stack edge feature vector
                edge_attr = torch.tensor(
                    np.stack(
                        [log_delta_r[i], log_mass2[i], log_kt[i], log_z[i], log_pt_jj[i], eta_jj[i], phi_jj[i]], axis=-1
                    )
                )
                # undirected edge index
                edge_index = torch.tensor(edge_indices[i].to_list(), dtype=torch.long).t().contiguous()
                # get edge match target
                y = torch.tensor(edge_match[i], dtype=torch.long)

                data = Data(x=x, edge_attr=edge_attr, edge_index=edge_index, y=y)
                data_list.append(data)

        if self.pre_filter is not None:
            data_list = [data for data in data_list if self.pre_filter(data)]

        if self.pre_transform is not None:
            data_list = [self.pre_transform(data) for data in data_list]

        data, slices = self.collate(data_list)
        torch.save((data, slices), self.processed_paths[0])
        logging.info(f"Total events saved: {len(data_list)}")


if __name__ == "__main__":
    root = osp.join(osp.dirname(osp.realpath(__file__)), "..", "..", "data/tmp")
    dataset = HHHGraph(root=root, entry_start=0, entry_stop=100)
